"""Reading to-dos out of an email body.

The model's judgement is not testable here -- what is testable is everything
around it: that a plausible reply becomes the right rows, that an implausible
one is rejected rather than filed on the wrong day, and that re-reading the
same message neither duplicates a commitment nor resurrects one the user has
already dealt with.

The worked example throughout is the one that prompted the feature: a Co-op
reminder that names a presentation *and* a progress report, where the single
`deadline` field could only ever hold one of the two.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from app import db, pipeline, planner
from app.llm import registry
from app.llm.base import Classification, ExtractedTask, LLMProvider, parse_classifications, parse_tasks


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.set_setting("user_address", "you@example.com")
    return tmp_path


COOP_BODY = """Dear Sam Rivera,

This is a reminder about your placement. Your Progress Report is due on
30 August 2026 and must be submitted through the portal. The Final Presentation
will take place on 12 September 2026 in Room 4213.

Best regards,
Placements Office
"""


def _add_email(email_id="coop", subject="Placement final presentation reminder - Sam Rivera",
               body=COOP_BODY, sender="placements@example.edu"):
    db.upsert_emails([{
        "id": email_id, "conversation_id": "", "subject": subject,
        "from_name": "Placements Office", "from_address": sender,
        "to_recipients": '["you@example.com"]', "cc_recipients": "[]",
        "received_at": "2026-08-20T09:00:00+00:00",
        "is_read": 0, "is_answered": 0, "is_flagged": 0, "has_attachments": 0,
        "importance": "normal", "web_link": "", "folder": "INBOX",
        "body_preview": body[:200], "body_text": body, "body_html": "",
        "synced_at": db.now_iso(),
    }])


class StubProvider(LLMProvider):
    """Stands in for Copilot. Returns whatever reply the test hands it, so the
    parsing and persistence around the model are exercised for real."""
    name = "stub"
    model = "stub-1"

    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    async def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return self.reply


def _reply(tasks, deadline="2026-09-12", bucket="action", email_id="coop"):
    return json.dumps([{
        "id": email_id, "bucket": bucket, "deadline": deadline,
        "tasks": tasks, "matched": [], "rationale": "placement deliverables due.",
    }])


def _classify_with(monkeypatch, reply):
    provider = StubProvider(reply)
    monkeypatch.setattr(registry, "get_provider", lambda: provider)
    monkeypatch.setattr(pipeline, "get_provider", lambda: provider)
    return asyncio.run(pipeline.classify_pending()), provider


def _reread_with(monkeypatch, reply):
    """Read the same mail again. classify_pending deliberately skips anything
    already classified -- that cache is the whole reason a rescan exists -- so
    a genuine second read has to go through rescan, exactly as the button does."""
    provider = StubProvider(reply)
    monkeypatch.setattr(registry, "get_provider", lambda: provider)
    monkeypatch.setattr(pipeline, "get_provider", lambda: provider)
    return asyncio.run(pipeline.rescan()), provider


# --------------------------------------------------------------------------
# parsing the model's answer
# --------------------------------------------------------------------------

def test_several_obligations_in_one_email_all_survive_parsing():
    tasks = parse_tasks([
        {"title": "Submit the placement progress report", "due": "2026-08-30"},
        {"title": "Present the placement final presentation", "due": "2026-09-12"},
    ])
    assert [(t.title, t.due_date) for t in tasks] == [
        ("Submit the placement progress report", "2026-08-30"),
        ("Present the placement final presentation", "2026-09-12"),
    ]


def test_a_task_without_a_date_is_dropped_not_filed_under_today():
    """Filing an undated obligation on an arbitrary day is the worst outcome:
    the user trusts the calendar, so a wrong date is worse than no row."""
    assert parse_tasks([{"title": "Submit the report", "due": None}]) == []
    assert parse_tasks([{"title": "Submit the report"}]) == []
    assert parse_tasks([{"title": "Submit the report", "due": "soon"}]) == []
    assert parse_tasks([{"title": "Submit the report", "due": "2026-02-31"}]) == []


def test_a_task_without_a_title_is_dropped():
    assert parse_tasks([{"title": "   ", "due": "2026-08-30"}]) == []
    assert parse_tasks([{"due": "2026-08-30"}]) == []


def test_task_parsing_survives_junk():
    assert parse_tasks(None) == []
    assert parse_tasks("submit the report") == []
    assert parse_tasks([None, 3, "x"]) == []
    assert parse_tasks([{"title": "Do it", "due": "2026-08-30"}, "junk"])[0].title == "Do it"


def test_alternative_date_keys_are_accepted():
    for key in ("due", "due_date", "deadline"):
        assert parse_tasks([{"title": "Do it", key: "2026-08-30"}])[0].due_date == "2026-08-30"


def test_duplicate_tasks_are_collapsed():
    tasks = parse_tasks([
        {"title": "Submit the report", "due": "2026-08-30"},
        {"title": "submit the  report", "due": "2026-08-30"},
    ])
    assert len(tasks) == 1


def test_a_runaway_model_is_capped():
    tasks = parse_tasks([{"title": f"Task {i}", "due": "2026-08-30"} for i in range(40)])
    assert len(tasks) == 5


def test_titles_are_trimmed_not_left_unbounded():
    long_title = "Submit " + ("the very detailed progress report " * 20)
    assert len(parse_tasks([{"title": long_title, "due": "2026-08-30"}])[0].title) <= 120


def test_a_classification_without_tasks_still_parses():
    """Most mail has no to-dos, and older models may omit the key entirely."""
    raw = json.dumps([{"id": "a", "bucket": "fyi", "deadline": None, "rationale": ""}])
    assert parse_classifications(raw, ["a"])[0].tasks == []


# --------------------------------------------------------------------------
# what ends up on the calendar
# --------------------------------------------------------------------------

def test_the_coop_email_produces_both_obligations(store, monkeypatch):
    _add_email()
    _classify_with(monkeypatch, _reply([
        {"title": "Submit the placement progress report", "due": "2026-08-30"},
        {"title": "Present the placement final presentation", "due": "2026-09-12"},
    ]))

    aug = planner.entries(date(2026, 8, 30), date(2026, 8, 30))
    sep = planner.entries(date(2026, 9, 12), date(2026, 9, 12))
    assert [e["title"] for e in aug] == ["Submit the placement progress report"]
    assert [e["title"] for e in sep] == ["Present the placement final presentation"]


def test_the_subject_line_no_longer_takes_a_slot_of_its_own(store, monkeypatch):
    """Before this feature the calendar showed "Co-op Project Final Presentation
    Reminder" and nothing else -- a chip that tells you a date exists but not
    what you owe. Once the body has been read, the to-dos say it better."""
    _add_email()
    _classify_with(monkeypatch, _reply([
        {"title": "Submit the placement progress report", "due": "2026-08-30"},
    ]))
    everything = planner.entries(date(2026, 1, 1), date(2026, 12, 31))
    assert [e["title"] for e in everything] == ["Submit the placement progress report"]
    assert all(e["kind"] == "task" for e in everything)


def test_an_email_with_no_extractable_tasks_keeps_its_own_chip(store, monkeypatch):
    _add_email(email_id="plain", subject="Room booking", body="Please confirm by 28 August 2026.")
    _classify_with(monkeypatch, _reply([], deadline="2026-08-28", email_id="plain"))
    items = planner.entries(date(2026, 8, 28), date(2026, 8, 28))
    assert [(e["kind"], e["title"]) for e in items] == [("email", "Room booking")]


def test_a_task_from_mail_keeps_a_way_back_to_the_message(store, monkeypatch):
    _add_email()
    _classify_with(monkeypatch, _reply([
        {"title": "Submit the placement progress report", "due": "2026-08-30"},
    ]))
    entry = planner.entries(date(2026, 8, 30), date(2026, 8, 30))[0]
    assert entry["email_id"] == "coop"
    assert entry["origin"] == "email"
    assert entry["sender"] == "Placements Office"
    assert entry["sender_address"] == "placements@example.edu"


def test_a_hand_written_task_has_no_sender_and_no_source(store):
    db.add_task("Water the plants", "2026-08-30")
    entry = planner.entries(date(2026, 8, 30), date(2026, 8, 30))[0]
    assert entry["origin"] == "user"
    assert entry["email_id"] is None
    assert entry["sender_address"] is None


def test_ranking_uses_the_soonest_obligation_not_whichever_date_came_first(store, monkeypatch):
    """The model put the September presentation in `deadline`. The thing that
    actually bites is the report on the 30th, and that is what the email should
    be ranked on."""
    _add_email()
    _classify_with(monkeypatch, _reply(
        [{"title": "Submit the placement progress report", "due": "2026-08-30"}],
        deadline="2026-09-12",
    ))
    with db.connect() as conn:
        row = conn.execute("SELECT deadline FROM classifications WHERE email_id = 'coop'").fetchone()
    assert row["deadline"] == "2026-08-30"


# --------------------------------------------------------------------------
# reading the same email twice
# --------------------------------------------------------------------------

def test_re_reading_an_email_does_not_duplicate_its_tasks(store, monkeypatch):
    _add_email()
    reply = _reply([{"title": "Submit the placement progress report", "due": "2026-08-30"}])
    _classify_with(monkeypatch, reply)
    _reread_with(monkeypatch, reply)
    assert len(planner.entries(date(2026, 8, 30), date(2026, 8, 30))) == 1


def test_a_completed_task_is_not_resurrected_by_a_re_read(store, monkeypatch):
    _add_email()
    reply = _reply([{"title": "Submit the placement progress report", "due": "2026-08-30"}])
    _classify_with(monkeypatch, reply)
    entry = planner.entries(date(2026, 8, 30), date(2026, 8, 30))[0]
    planner.set_done("task", entry["id"], True)

    _reread_with(monkeypatch, reply)
    again = planner.entries(date(2026, 8, 30), date(2026, 8, 30))
    assert len(again) == 1
    assert again[0]["done"] is True, "it must not come back as unfinished work"


def test_a_removed_task_is_not_resurrected_by_a_re_read(store, monkeypatch):
    _add_email()
    reply = _reply([{"title": "Submit the placement progress report", "due": "2026-08-30"}])
    _classify_with(monkeypatch, reply)
    entry = planner.entries(date(2026, 8, 30), date(2026, 8, 30))[0]
    planner.remove("task", entry["id"])

    _reread_with(monkeypatch, reply)
    assert planner.entries(date(2026, 8, 30), date(2026, 8, 30)) == [], \
        "and the email's own subject must not take its place on that day"
    assert len(planner.removed()) == 1


def test_a_task_the_mail_no_longer_mentions_is_cleared_away(store, monkeypatch):
    """The email was corrected and re-sent, or the model read it better the
    second time. An untouched to-do that is no longer in the message goes."""
    _add_email()
    _classify_with(monkeypatch, _reply([
        {"title": "Submit the placement progress report", "due": "2026-08-30"},
        {"title": "Book a room for the rehearsal", "due": "2026-09-01"},
    ]))
    assert len(planner.entries(date(2026, 8, 1), date(2026, 9, 30))) == 2

    _reread_with(monkeypatch, _reply([
        {"title": "Submit the placement progress report", "due": "2026-08-30"},
    ]))
    left = planner.entries(date(2026, 8, 1), date(2026, 9, 30))
    assert [e["title"] for e in left] == ["Submit the placement progress report"]


def test_a_task_the_user_ticked_is_kept_even_if_the_mail_stops_mentioning_it(store, monkeypatch):
    _add_email()
    _classify_with(monkeypatch, _reply([
        {"title": "Book a room for the rehearsal", "due": "2026-09-01"},
    ]))
    entry = planner.entries(date(2026, 9, 1), date(2026, 9, 1))[0]
    planner.set_done("task", entry["id"], True)

    _reread_with(monkeypatch, _reply([]))
    assert len(planner.entries(date(2026, 9, 1), date(2026, 9, 1))) == 1, \
        "the user's record of having done it outranks the model changing its mind"


def test_hand_written_tasks_are_untouched_by_extraction(store, monkeypatch):
    _add_email()
    mine = db.add_task("Water the plants", "2026-08-30")
    _classify_with(monkeypatch, _reply([
        {"title": "Submit the placement progress report", "due": "2026-08-30"},
    ]))
    _reread_with(monkeypatch, _reply([]))
    assert db.get_task(mine["id"]) is not None


# --------------------------------------------------------------------------
# the fallback path
# --------------------------------------------------------------------------

def test_a_structural_pass_does_not_wipe_tasks_a_model_pass_found(store, monkeypatch):
    """The model found the report; the next sync ran with AI unavailable. A
    structural pass knows nothing about to-dos, and must not read its own
    silence as "there are none"."""
    _add_email()
    _classify_with(monkeypatch, _reply([
        {"title": "Submit the placement progress report", "due": "2026-08-30"},
    ]))

    pipeline._persist(
        [Classification(email_id="coop", bucket="action", deadline="2026-09-12")],
        {"coop": db.get_emails(["coop"])[0]},
        source="structural", model="none",
    )
    assert len(planner.entries(date(2026, 8, 30), date(2026, 8, 30))) == 1


def test_an_llm_pass_with_no_tasks_does_clear_them(store, monkeypatch):
    """Unlike the structural fallback, a model that looked and found nothing is
    an answer, not an absence."""
    _add_email()
    _classify_with(monkeypatch, _reply([
        {"title": "Submit the placement progress report", "due": "2026-08-30"},
    ]))
    _reread_with(monkeypatch, _reply([], deadline=None))
    assert planner.entries(date(2026, 8, 30), date(2026, 8, 30)) == []


# --------------------------------------------------------------------------
# muting reaches through to extracted to-dos
# --------------------------------------------------------------------------

def test_muting_a_sender_takes_its_extracted_tasks_too(store, monkeypatch):
    _add_email(sender="noreply@promo.io")
    _classify_with(monkeypatch, _reply([
        {"title": "Register for the webinar", "due": "2026-08-30"},
    ]))
    assert len(planner.entries(date(2026, 8, 30), date(2026, 8, 30))) == 1

    db.mute_sender("noreply@promo.io")
    assert planner.entries(date(2026, 8, 30), date(2026, 8, 30)) == []

    db.unmute_sender("noreply@promo.io")
    assert len(planner.entries(date(2026, 8, 30), date(2026, 8, 30))) == 1


def test_muting_never_hides_a_hand_written_task(store):
    db.add_task("Water the plants", "2026-08-30")
    db.mute_sender("anyone@anywhere.com")
    assert len(planner.entries(date(2026, 8, 30), date(2026, 8, 30))) == 1


# --------------------------------------------------------------------------
# rescan
# --------------------------------------------------------------------------

def test_rescan_re_reads_mail_that_was_already_classified(store, monkeypatch):
    """A mailbox classified before this feature existed has no to-dos and would
    never grow any, because classification is cached and never revisited."""
    _add_email()
    _classify_with(monkeypatch, _reply([], deadline=None))
    assert planner.entries(date(2026, 8, 1), date(2026, 9, 30)) == []

    provider = StubProvider(_reply([
        {"title": "Submit the placement progress report", "due": "2026-08-30"},
    ]))
    monkeypatch.setattr(pipeline, "get_provider", lambda: provider)
    result = asyncio.run(pipeline.rescan())

    assert result["classified"] == 1
    assert [e["title"] for e in planner.entries(date(2026, 8, 1), date(2026, 9, 30))] == [
        "Submit the placement progress report"
    ]


def test_rescan_terminates_on_an_empty_mailbox(store, monkeypatch):
    provider = StubProvider("[]")
    monkeypatch.setattr(pipeline, "get_provider", lambda: provider)
    assert asyncio.run(pipeline.rescan())["classified"] == 0


def test_rescan_does_not_lose_what_the_user_already_decided(store, monkeypatch):
    _add_email()
    reply = _reply([{"title": "Submit the placement progress report", "due": "2026-08-30"}])
    _classify_with(monkeypatch, reply)
    entry = planner.entries(date(2026, 8, 30), date(2026, 8, 30))[0]
    planner.set_done("task", entry["id"], True)
    db.set_feedback("coop", "pinned")

    provider = StubProvider(reply)
    monkeypatch.setattr(pipeline, "get_provider", lambda: provider)
    asyncio.run(pipeline.rescan())

    assert planner.entries(date(2026, 8, 30), date(2026, 8, 30))[0]["done"] is True
    assert db.all_feedback()["coop"]["verdict"] == "pinned"
