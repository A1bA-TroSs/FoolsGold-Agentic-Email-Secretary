"""When a deadline stops being a reminder.

A due date a month past is history: either it was met, or it was missed and
the world moved on. Announcing "496 days overdue" as the most urgent thing of
the morning only teaches you to ignore the briefing.

Note what this is *not*: it is not about how old the email is, and it does not
remove anything. The message stays in the inbox, the entry stays on the
calendar on the day it was due. It simply stops being introduced as something
due today.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from app import db, pipeline, planner, priority


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return tmp_path


TODAY = date(2026, 8, 27)


def _email(email_id: str, subject: str, days_ago: int | None, bucket: str = "action", score: float = 10.0):
    """days_ago counts back from TODAY; None means no deadline at all."""
    db.upsert_emails([{
        "id": email_id, "conversation_id": "", "subject": subject,
        "from_name": "Someone", "from_address": f"{email_id}@example.edu",
        "to_recipients": "[]", "cc_recipients": "[]",
        "received_at": "2026-08-20T09:00:00+00:00",
        "is_read": 0, "is_answered": 0, "is_flagged": 0, "has_attachments": 0,
        "importance": "normal", "web_link": "", "folder": "INBOX",
        "body_preview": "", "body_text": "", "body_html": "",
        "synced_at": db.now_iso(),
    }])
    deadline = (TODAY - timedelta(days=days_ago)).isoformat() if days_ago is not None else None
    db.save_classification({
        "email_id": email_id, "bucket": bucket, "deadline": deadline,
        "rationale": "", "score": score, "matched": "", "model": "",
        "source": "structural", "created_at": db.now_iso(),
    })


# --------------------------------------------------------------------------
# the rule itself
# --------------------------------------------------------------------------

def test_the_horizon_is_a_month():
    assert priority.FORGET_AFTER_DAYS == 30


def test_a_deadline_inside_the_month_is_still_live():
    for days in (0, 1, 7, 29, 30):
        assert priority.is_forgotten((TODAY - timedelta(days=days)).isoformat(), TODAY) is False, days


def test_a_deadline_past_the_month_is_forgotten():
    for days in (31, 60, 496):
        assert priority.is_forgotten((TODAY - timedelta(days=days)).isoformat(), TODAY) is True, days


def test_a_future_deadline_is_never_forgotten():
    assert priority.is_forgotten((TODAY + timedelta(days=400)).isoformat(), TODAY) is False


def test_no_deadline_and_junk_are_not_forgotten_they_are_simply_absent():
    for value in (None, "", "someday", "2026-02-31"):
        assert priority.is_forgotten(value, TODAY) is False


# --------------------------------------------------------------------------
# the briefing
# --------------------------------------------------------------------------

def test_the_oldest_deadline_no_longer_leads_the_briefing(store):
    """The exact bug. The structural briefing sorts deadlines ascending, so
    with no lower bound the single oldest date in the mailbox was row one every
    morning -- announced as the most pressing thing of the day."""
    _email("ancient", "Something from last year", days_ago=496, score=5.0)
    _email("real", "Due last week", days_ago=6, score=4.0)

    _, rows = pipeline._fallback_agenda(pipeline._digest_candidates(today=TODAY), today=TODAY)
    titles = [r["subject"] for r in rows]
    assert titles[0] == "Due last week"
    assert "Something from last year" not in titles[:1]


def test_a_forgotten_deadline_is_not_presented_as_a_deadline(store):
    _email("ancient", "Something from last year", days_ago=496)
    candidates = pipeline._digest_candidates(today=TODAY)
    assert candidates[0]["deadline"] is None, "blanked, so no path can call it due"


def test_the_email_itself_is_still_a_candidate(store):
    """Forgetting the date is not the same as hiding the mail. It may still be
    worth raising -- it just gets there on its own merits."""
    _email("ancient", "Something from last year", days_ago=496)
    assert [c["id"] for c in pipeline._digest_candidates(today=TODAY)] == ["ancient"]


def test_a_deadline_just_inside_the_month_still_leads(store):
    _email("recent", "Missed three weeks ago", days_ago=21, score=5.0)
    _email("other", "No date", None, score=9.0)
    _, rows = pipeline._fallback_agenda(pipeline._digest_candidates(today=TODAY), today=TODAY)
    assert rows[0]["subject"] == "Missed three weeks ago"
    assert rows[0]["deadline"] is not None


def test_the_boundary_day_is_still_raised(store):
    _email("edge", "Exactly a month ago", days_ago=30)
    assert pipeline._digest_candidates(today=TODAY)[0]["deadline"] is not None
    _email("over", "A month and a day ago", days_ago=31)
    by_id = {c["id"]: c for c in pipeline._digest_candidates(today=TODAY)}
    assert by_id["over"]["deadline"] is None


# --------------------------------------------------------------------------
# the 9am / 9pm reminder
# --------------------------------------------------------------------------

def test_a_reminder_carries_missed_work_forward_but_not_forever(store):
    db.add_task("Missed last week", (TODAY - timedelta(days=7)).isoformat())
    db.add_task("Missed last year", (TODAY - timedelta(days=496)).isoformat())

    overdue = [e["title"] for e in planner.agenda(TODAY)["overdue"]]
    assert overdue == ["Missed last week"]


def test_the_reminder_window_matches_the_briefing(store):
    db.add_task("Just inside", (TODAY - timedelta(days=30)).isoformat())
    db.add_task("Just outside", (TODAY - timedelta(days=31)).isoformat())
    overdue = {e["title"] for e in planner.agenda(TODAY)["overdue"]}
    assert overdue == {"Just inside"}


# --------------------------------------------------------------------------
# what forgetting does NOT do
# --------------------------------------------------------------------------

def test_the_entry_stays_on_the_calendar_where_it_happened(store):
    """The calendar is a record of the year as well as a plan for the week.
    Nothing is removed -- it just stops claiming anything about today."""
    old = TODAY - timedelta(days=496)
    db.add_task("Missed last year", old.isoformat())
    items = planner.entries(old, old)
    assert [i["title"] for i in items] == ["Missed last year"]


def test_an_ancient_deadline_still_scores_nothing(store):
    """The scoring curve reached zero at three weeks long before this rule
    existed; forgetting is about what we *say*, not a second way to rank."""
    assert priority.deadline_points((TODAY - timedelta(days=496)).isoformat(), TODAY) == 0.0
    assert priority.deadline_points((TODAY - timedelta(days=1)).isoformat(), TODAY) > 0


# --------------------------------------------------------------------------
# the cache that hid this fix
# --------------------------------------------------------------------------

def test_a_briefing_built_by_older_rules_is_rebuilt_not_served(store):
    """The digest is cached for a calendar day. Without a version stamp, a fix
    to what belongs in a briefing stays invisible until tomorrow -- which is
    exactly how one kept announcing a 2022 deadline hours after the code that
    produced it had been replaced."""
    import json
    day = date.today().isoformat()
    stale = json.dumps({
        "headline": "built by yesterday's rules",
        "items": [{"email_id": "x", "subject": "Something from 2022", "deadline": "2022-11-24"}],
        "logic_version": pipeline.DIGEST_LOGIC_VERSION - 1,
    })
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO digests (day, body, model, created_at) VALUES (?, ?, 'structural', ?)",
            (day, stale, db.now_iso()),
        )
        conn.commit()

    assert pipeline.cached_digest(day) is None


def test_a_briefing_built_by_the_current_rules_is_reused(store):
    import json
    day = date.today().isoformat()
    fresh = json.dumps({
        "headline": "today's", "items": [],
        "logic_version": pipeline.DIGEST_LOGIC_VERSION,
    })
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO digests (day, body, model, created_at) VALUES (?, ?, 'structural', ?)",
            (day, fresh, db.now_iso()),
        )
        conn.commit()

    cached = pipeline.cached_digest(day)
    assert cached is not None and cached["headline"] == "today's"


def test_a_pre_versioning_markdown_row_is_rebuilt(store):
    """The oldest rows hold plain markdown and no version at all."""
    day = date.today().isoformat()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO digests (day, body, model, created_at) VALUES (?, ?, 'llm', ?)",
            (day, "# Yesterday's markdown briefing", db.now_iso()),
        )
        conn.commit()
    assert pipeline.cached_digest(day) is None


def test_a_dated_email_outside_the_first_five_is_still_described_as_dated(store):
    """It used to fall through to "needs a reply" while its chip still showed a
    deadline -- the row contradicting itself."""
    for i in range(7):
        _email(f"e{i}", f"Dated {i}", days_ago=-(i + 1))   # all in the future
    _, rows = pipeline._fallback_agenda(
        pipeline._digest_candidates(today=TODAY), today=TODAY
    )
    assert len(rows) == 7
    assert all(r["deadline"] for r in rows)
    assert all(r["note_key"] in ("dueOn", "dueToday") for r in rows), \
        [r["note_key"] for r in rows]
