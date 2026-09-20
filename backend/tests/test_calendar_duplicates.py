"""One chip per announcement.

Written from the real mailbox: a career centre sent the same ICAC seminar three
times -- 8th, 11th, 17th September -- and the classifier read a deadline out of
each. The 11th produced a to-do, so its own chip was already suppressed. The
other two both landed on the 20th, and the day showed the same title twice in a
row with nothing to tell them apart.
"""
from __future__ import annotations

from datetime import date

import pytest

from app import db, planner

SUBJECT = "“Ethics in Practice” ICAC Seminar"
SENDER = "careers@uni.edu"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    db.init_db()

    def add(ident: str, subject: str, received: str, deadline: str,
            bucket: str = "fyi", sender: str = SENDER) -> None:
        db.upsert_emails([dict(
            id=ident, conversation_id=None, subject=subject, from_name="Career Centre",
            from_address=sender, to_recipients="[]", cc_recipients="[]",
            received_at=f"{received}T09:00:00+00:00", is_read=1, is_answered=0,
            is_flagged=0, has_attachments=0, importance="normal", web_link="",
            folder="INBOX", body_preview="", body_text="", body_html="",
            source_path="", synced_at=db.now_iso())])
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO classifications (email_id, bucket, deadline, rationale, "
                "score, matched, model, source, created_at) "
                "VALUES (?, ?, ?, '', 50, '', 'test', 'test', ?)",
                (ident, bucket, deadline, db.now_iso()))
            conn.commit()
    return add


def on_day(day: str) -> list[dict]:
    return between(day, day)


def between(start: str, end: str) -> list[dict]:
    got = planner.entries(date.fromisoformat(start), date.fromisoformat(end))
    return [e for e in got if e["kind"] == "email"]


def test_the_same_announcement_sent_twice_is_one_chip(store):
    store("e1", SUBJECT, "2026-09-08", "2026-09-20")
    store("e2", SUBJECT, "2026-09-17", "2026-09-20")
    got = on_day("2026-09-20")
    assert len(got) == 1
    assert got[0]["title"] == SUBJECT


def test_the_surviving_chip_is_the_most_recent_reminder(store):
    """A reminder supersedes what it reminds you of: its wording is current,
    and so is the app's own reading of it."""
    store("old", SUBJECT, "2026-09-08", "2026-09-20")
    store("new", SUBJECT, "2026-09-17", "2026-09-20", bucket="action")
    got = on_day("2026-09-20")
    assert [e["id"] for e in got] == ["new"]
    assert got[0]["bucket"] == "action"


def test_the_chip_says_how_many_messages_it_stands_for(store):
    store("a", SUBJECT, "2026-09-08", "2026-09-20")
    store("b", SUBJECT, "2026-09-11", "2026-09-20")
    store("c", SUBJECT, "2026-09-17", "2026-09-20")
    assert on_day("2026-09-20")[0]["copies"] == 3


def test_a_reminder_sent_as_a_reply_is_the_same_announcement(store):
    """Departments often remind you by replying to their own announcement."""
    store("first", SUBJECT, "2026-09-08", "2026-09-20")
    store("again", f"Re: {SUBJECT}", "2026-09-17", "2026-09-20")
    assert len(on_day("2026-09-20")) == 1


@pytest.mark.parametrize("variant", [
    f"  {SUBJECT}  ",
    f"{SUBJECT}.",
    SUBJECT.upper(),
    f"FW: {SUBJECT}",
])
def test_cosmetic_differences_do_not_make_a_second_event(store, variant):
    store("a", SUBJECT, "2026-09-08", "2026-09-20")
    store("b", variant, "2026-09-17", "2026-09-20")
    assert len(on_day("2026-09-20")) == 1


def test_the_same_title_on_a_different_day_is_a_different_occurrence(store):
    """A weekly seminar is a real recurrence. Collapsing on the subject alone
    would hide events rather than duplicates -- which is worse than the bug.

    Queried as a *range*, not a day at a time: asking for one day at a time
    never puts the two occurrences in front of the collapser, so it cannot
    fail however wrong the key is. That is how this test first passed against
    a subject-only key."""
    store("wk1", "Engineering Seminar Series", "2026-09-08", "2026-09-20")
    store("wk2", "Engineering Seminar Series", "2026-09-15", "2026-09-27")
    got = between("2026-09-01", "2026-09-30")
    assert [e["due"] for e in got] == ["2026-09-20", "2026-09-27"]
    assert all(e["copies"] == 1 for e in got)


def test_two_different_events_on_one_day_both_survive(store):
    store("a", SUBJECT, "2026-09-17", "2026-09-20")
    store("b", "Careers fair", "2026-09-17", "2026-09-20")
    assert len(on_day("2026-09-20")) == 2


def test_two_senders_announcing_the_same_thing_still_collapse(store):
    """Deliberate, and the reason the sender is *not* in the key. One event
    reaching the user through two mailing lists is the commonest way a thing
    arrives twice, and they still only have to attend it once."""
    store("a", SUBJECT, "2026-09-08", "2026-09-20", sender="careers@uni.edu")
    store("b", SUBJECT, "2026-09-17", "2026-09-20", sender="students@uni.edu")
    got = on_day("2026-09-20")
    assert len(got) == 1
    assert got[0]["copies"] == 2
    assert got[0]["sender_address"] == "students@uni.edu", "the later one survives"


def test_collapsing_does_not_leak_the_sort_field_into_the_payload(store):
    """`received_at` is read by the collapser and by nothing else. Leaving it
    on the entry makes it look like part of the contract the UI can rely on."""
    store("a", SUBJECT, "2026-09-17", "2026-09-20")
    assert "received_at" not in on_day("2026-09-20")[0]


def test_an_email_already_read_into_a_todo_is_still_suppressed(store):
    """The pre-existing rule has to survive the new one: the to-do is the thing
    you actually have to do, and it is already on that day."""
    store("e1", SUBJECT, "2026-09-11", "2026-09-20")
    db.sync_email_tasks("e1", [{"title": "Register for the ICAC seminar",
                                "due_date": "2026-09-20"}])
    assert on_day("2026-09-20") == []
