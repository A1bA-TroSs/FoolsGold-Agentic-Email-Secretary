"""Can the app tell "nothing arrived" from "this has been broken for days"?

It could not. The background poller caught every exception and dropped it, so
a mailbox frozen on 14 September rendered exactly like a quiet week, and there
was nowhere to look it up. These pin the three states apart.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.routers.mail import sync_state


def _email(eid, received):
    return dict(id=eid, conversation_id=None, subject="s", from_name="n",
                from_address="a@b.com", to_recipients="[]", cc_recipients="[]",
                received_at=received, is_read=1, is_answered=0, is_flagged=0,
                has_attachments=0, importance="normal", web_link="", folder="INBOX",
                body_preview="", body_text="", body_html="", synced_at=db.now_iso())


def _ago(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return tmp_path


def _successful_checks(n, spread_hours, newest):
    """Simulate n successful syncs, the oldest `spread_hours` ago, all seeing
    the same newest message."""
    with db.connect() as conn:
        for i in range(n):
            when = _ago(spread_hours * (n - 1 - i) / max(1, n - 1))
            conn.execute(
                "INSERT INTO sync_events (started_at, finished_at, trigger, ok, "
                "scanned, fetched, written, newest_received) "
                "VALUES (?,?, 'poller', 1, 10, 5, 0, ?)", (when, when, newest))
        conn.commit()


def test_a_quiet_hour_is_not_reported_as_a_problem(store):
    newest = _ago(2)
    db.upsert_emails([_email("a", newest)])
    _successful_checks(2, 1, newest)
    assert sync_state()["state"] == "ok"


def test_a_source_that_stopped_producing_is_named_as_frozen(store):
    """The real case: Apple Mail is closed, so its files stop changing. Every
    sync succeeds and writes rows, and the newest message never moves."""
    newest = _ago(5 * 24)
    db.upsert_emails([_email("a", newest)])
    _successful_checks(60, 5 * 24, newest)

    state = sync_state()
    assert state["state"] == "frozen", state
    assert state["frozen_checks"] >= 3
    assert state["frozen_hours"] >= 12
    # The number the user needs is "since when", not "something is wrong".
    assert state["newest_received"] == newest


def test_a_failing_sync_says_so_and_says_for_how_long(store):
    db.upsert_emails([_email("a", _ago(1))])
    for _ in range(4):
        db.record_sync("poller", db.now_iso(), ok=False, error="PermissionError: ~/Library/Mail")
    state = sync_state()
    assert state["state"] == "failing"
    assert state["consecutive_failures"] == 4
    assert "PermissionError" in state["error"]


def test_one_success_clears_the_failure_run(store):
    db.upsert_emails([_email("a", _ago(1))])
    db.record_sync("poller", db.now_iso(), ok=False, error="blip")
    db.record_sync("poller", db.now_iso(), ok=True, written=1)
    state = sync_state()
    assert state["state"] == "ok"
    assert state["consecutive_failures"] == 0


def test_the_history_is_bounded(store):
    """A diagnostic tail, not a history. 200 rows is plenty to answer "how long
    has this been failing" and small enough never to matter."""
    for _ in range(260):
        db.record_sync("poller", db.now_iso(), ok=True)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sync_events").fetchone()[0] <= 201


def test_a_fresh_install_does_not_claim_to_be_broken(store):
    assert sync_state()["state"] == "unknown"
