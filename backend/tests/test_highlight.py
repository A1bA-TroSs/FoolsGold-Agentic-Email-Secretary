"""Highlighting a sender — the opposite instruction to muting.

Highlighting is the opposite instruction to muting, and the two have to stay
in step between the mailbox and the calendar.

Both are statements about a *correspondent*, not a message, so both live in
their own table and every surface consults the same one. That is what makes
them stay in sync: there is no second copy to drift.
"""
from __future__ import annotations

from datetime import date

import pytest

from app import db, planner


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return tmp_path


def _email(email_id: str, sender: str, subject: str, deadline: str | None = None):
    db.upsert_emails([{
        "id": email_id, "conversation_id": "", "subject": subject,
        "from_name": sender.split("@")[0].title(), "from_address": sender,
        "to_recipients": "[]", "cc_recipients": "[]",
        "received_at": "2026-08-20T09:00:00+00:00",
        "is_read": 0, "is_answered": 0, "is_flagged": 0, "has_attachments": 0,
        "importance": "normal", "web_link": "", "folder": "INBOX",
        "body_preview": "", "body_text": "", "body_html": "",
        "synced_at": db.now_iso(),
    }])
    db.save_classification({
        "email_id": email_id, "bucket": "action", "deadline": deadline,
        "rationale": "", "score": 10.0, "matched": "", "model": "",
        "source": "structural", "created_at": db.now_iso(),
    })


# --------------------------------------------------------------------------
# it is about the sender, not the message
# --------------------------------------------------------------------------

def test_highlighting_one_email_colours_every_email_from_that_sender(store):
    """The whole point: you colour a sender once, from whichever message
    happened to be in front of you."""
    _email("a", "supervisor@example.edu", "Chapter 3")
    _email("b", "supervisor@example.edu", "Chapter 4")
    _email("c", "other@example.edu", "Unrelated")

    db.highlight_sender("supervisor@example.edu", "green")
    highlights = db.sender_highlights()
    assert highlights == {"supervisor@example.edu": "green"}


def test_addresses_normalise_to_lowercase(store):
    db.highlight_sender("Supervisor@Example.EDU", "blue")
    assert db.sender_highlights() == {"supervisor@example.edu": "blue"}
    db.unhighlight_sender("SUPERVISOR@example.edu")
    assert db.sender_highlights() == {}


def test_choosing_a_second_colour_replaces_the_first(store):
    db.highlight_sender("supervisor@example.edu", "red")
    db.highlight_sender("supervisor@example.edu", "yellow")
    assert db.sender_highlights() == {"supervisor@example.edu": "yellow"}


def test_only_the_offered_colours_are_accepted(store):
    """A free hex value chosen against the ivory theme would be unreadable in
    the dark one, so the palette is closed."""
    for colour in db.HIGHLIGHT_COLORS:
        db.highlight_sender("supervisor@example.edu", colour)
    for bad in ("#ff0000", "chartreuse", "", None, "RED"):
        with pytest.raises(ValueError):
            db.highlight_sender("supervisor@example.edu", bad)


def test_an_empty_address_is_ignored_not_stored(store):
    db.highlight_sender("   ", "red")
    assert db.sender_highlights() == {}


# --------------------------------------------------------------------------
# highlight and mute are opposites
# --------------------------------------------------------------------------

def test_highlighting_a_muted_sender_unmutes_them(store):
    """You cannot both want an address out of the way and want it to catch your
    eye. The later instruction wins."""
    _email("a", "supervisor@example.edu", "Chapter 3")
    db.mute_sender("supervisor@example.edu")
    assert db.muted_senders() == {"supervisor@example.edu"}

    db.highlight_sender("supervisor@example.edu", "green")
    assert db.muted_senders() == set()
    assert db.sender_highlights() == {"supervisor@example.edu": "green"}


def test_muting_a_highlighted_sender_keeps_the_colour_for_later(store):
    """Muting says "not now", not "forget my colour" -- so unmuting restores
    the choice instead of silently losing it."""
    db.highlight_sender("supervisor@example.edu", "green")
    db.mute_sender("supervisor@example.edu")
    assert db.sender_highlights() == {"supervisor@example.edu": "green"}

    db.unmute_sender("supervisor@example.edu")
    assert db.sender_highlights() == {"supervisor@example.edu": "green"}


# --------------------------------------------------------------------------
# the calendar sees the same thing the mailbox does
# --------------------------------------------------------------------------

def test_a_calendar_entry_wears_its_senders_colour(store):
    _email("a", "supervisor@example.edu", "Chapter 3", deadline="2026-08-30")
    db.highlight_sender("supervisor@example.edu", "green")
    entry = planner.entries(date(2026, 8, 30), date(2026, 8, 30))[0]
    assert entry["highlight"] == "green"


def test_an_unhighlighted_sender_carries_no_colour(store):
    _email("a", "supervisor@example.edu", "Chapter 3", deadline="2026-08-30")
    assert planner.entries(date(2026, 8, 30), date(2026, 8, 30))[0]["highlight"] is None


def test_a_todo_read_out_of_highlighted_mail_wears_the_colour_too(store):
    """Otherwise the calendar would colour the message but not the work the
    message created -- and after extraction it is the work that is shown."""
    _email("a", "supervisor@example.edu", "Chapter 3")
    db.add_task("Submit chapter 3", "2026-08-30", email_id="a", origin="email")
    db.highlight_sender("supervisor@example.edu", "purple")
    entry = planner.entries(date(2026, 8, 30), date(2026, 8, 30))[0]
    assert entry["kind"] == "task"
    assert entry["highlight"] == "purple"


def test_a_hand_written_task_can_never_be_highlighted(store):
    """It has no sender, so there is nothing to colour by."""
    db.add_task("Water the plants", "2026-08-30")
    entry = planner.entries(date(2026, 8, 30), date(2026, 8, 30))[0]
    assert entry["highlight"] is None
    assert entry["sender_address"] is None


def test_unhighlighting_clears_the_calendar_too(store):
    _email("a", "supervisor@example.edu", "Chapter 3", deadline="2026-08-30")
    db.highlight_sender("supervisor@example.edu", "green")
    db.unhighlight_sender("supervisor@example.edu")
    assert planner.entries(date(2026, 8, 30), date(2026, 8, 30))[0]["highlight"] is None


def test_highlighting_from_the_calendar_reaches_a_muted_senders_mail(store):
    """The two surfaces share one table, so an instruction given in either is
    already true in the other -- including the un-mute that comes with it."""
    _email("a", "promo@shop.io", "Offer", deadline="2026-08-30")
    db.mute_sender("promo@shop.io")
    assert planner.entries(date(2026, 8, 30), date(2026, 8, 30)) == []

    db.highlight_sender("promo@shop.io", "orange")
    back = planner.entries(date(2026, 8, 30), date(2026, 8, 30))
    assert len(back) == 1 and back[0]["highlight"] == "orange"


# --------------------------------------------------------------------------
# the accounting the box shows
# --------------------------------------------------------------------------

def test_the_highlight_box_counts_what_it_covers(store):
    _email("a", "supervisor@example.edu", "Chapter 3")
    _email("b", "supervisor@example.edu", "Chapter 4")
    db.highlight_sender("supervisor@example.edu", "green")
    rows = db.highlighted_sender_rows()
    assert len(rows) == 1
    assert rows[0]["address"] == "supervisor@example.edu"
    assert rows[0]["color"] == "green"
    assert rows[0]["message_count"] == 2
    assert rows[0]["display_name"] == "Supervisor"


def test_a_highlighted_sender_with_no_mail_yet_still_lists(store):
    db.highlight_sender("future@example.edu", "blue")
    rows = db.highlighted_sender_rows()
    assert rows[0]["message_count"] == 0
