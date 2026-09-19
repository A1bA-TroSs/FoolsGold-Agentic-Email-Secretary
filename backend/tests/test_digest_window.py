"""What "today" means in a briefing headed "What's crucial today".

Reported from a real screen: the top three items were 2 days overdue, due
tomorrow, due tomorrow — and the overdue one led. On a mailbox with any backlog
the score alone puts last week's crisis above this afternoon's, because score
mixes urgency with relevance and a strongly-relevant stale item outranks a
moderately-relevant live one.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app import db, pipeline

TODAY = date(2026, 9, 20)


def d(n):
    return (TODAY + timedelta(days=n)).isoformat()


def rank(deadline, score):
    return pipeline._digest_rank(deadline, score, TODAY)


def test_due_today_leads_even_against_a_higher_scoring_overdue_item():
    assert rank(d(0), 10) < rank(d(-9), 99)


def test_inside_the_window_the_nearer_deadline_wins():
    assert rank(d(0), 10) < rank(d(3), 90) < rank(d(6), 90)


def test_recently_missed_still_counts_but_below_anything_still_catchable():
    """Overdue by a day is usually live. Overdue by nine is a thing you missed,
    not a thing today."""
    assert rank(d(6), 10) < rank(d(-1), 99), "due this week beats 1 day overdue"
    assert rank(d(-1), 10) < rank(d(-9), 99), "1 day overdue beats 9 days overdue"


def test_beyond_the_window_falls_back_to_the_ranking():
    """Outside the window there is no date argument left, so the existing
    priority order decides -- including against undated mail."""
    assert rank(None, 90) < rank(None, 40)
    assert rank(None, 90) < rank(d(30), 40)


def test_it_is_an_ordering_not_a_filter():
    """When nothing is due this week the briefing must still say something.
    Every item gets a finite key; none is excluded."""
    for deadline in (None, d(-90), d(365), "not-a-date"):
        assert isinstance(rank(deadline, 1.0), tuple)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    db.init_db()
    return tmp_path


def _add(eid, deadline, score, bucket="action"):
    db.upsert_emails([dict(
        id=eid, conversation_id=None, subject=eid, from_name="X",
        from_address="x@uni.edu", to_recipients='["me@x.com"]', cc_recipients="[]",
        received_at="2026-09-18T09:00:00+00:00", is_read=0, is_answered=0,
        is_flagged=0, has_attachments=0, importance="normal", web_link="",
        folder="INBOX", body_preview="", body_text="", body_html="",
        synced_at=db.now_iso())])
    with db.connect() as conn:
        conn.execute("INSERT INTO classifications (email_id, bucket, deadline, score, source) "
                     "VALUES (?,?,?,?,'llm')", (eid, bucket, deadline, score))
        conn.commit()


def test_the_shortlist_is_not_decided_on_the_wrong_axis(store):
    """Taking the top N by score and *then* sorting by date would never promote
    a due-tomorrow email that sits below the cut. The query over-fetches so the
    date sort has something to promote."""
    for i in range(30):
        _add(f"hi{i}", d(-20), 100 - i)       # thirty stale, all high-scoring
    _add("tomorrow", d(1), 1.0)               # one live, lowest score in the set

    picked = pipeline._digest_candidates(limit=7, today=TODAY)
    assert picked and picked[0]["id"] == "tomorrow", [p["id"] for p in picked]


def test_a_deadline_long_past_is_not_introduced_as_a_deadline(store):
    _add("ancient", d(-200), 90)
    picked = pipeline._digest_candidates(limit=5, today=TODAY)
    assert picked[0]["deadline"] is None, "a forgotten date must not be presented as due"


def test_the_window_setting_moves_the_boundary(store):
    """The user's look-ahead window is the same number the briefing obeys, not
    a second constant that happens to agree with it today."""
    db.set_setting("deadline_horizon_days", "3")
    assert rank(d(2), 10) < rank(d(10), 99)       # inside 3 days: tier 0
    inside_3 = rank(d(2), 50)
    outside_3 = rank(d(5), 50)
    assert inside_3 < outside_3
    db.set_setting("deadline_horizon_days", "14")
    assert rank(d(5), 50) < outside_3, "widening the window promotes day 5"
