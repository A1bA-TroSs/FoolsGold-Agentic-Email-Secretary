"""A permission failure that has since been fixed must not keep saying so.

Regression (2026-09-21): after granting Full Disk Access and restarting, the
banner still read "sync failed 6 times -- macOS has not given FoolsGold access
..." (in English, cut off mid-sentence) until the next successful poll. To the
user that reads as "your fix didn't work".
"""
from __future__ import annotations

import pytest

from app import db
from app.routers import mail as mail_router
from app.sources.applemail import FULL_DISK_ACCESS_HINT
from app.sources.base import SourceStatus


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return tmp_path


class _Source:
    def __init__(self, ready):
        self.ready = ready

    def status(self):
        return SourceStatus(ready=self.ready)


def _fail(n, error):
    for _ in range(n):
        db.record_sync("poller", db.now_iso(), ok=False, error=error)


def test_a_permission_failure_is_a_key_not_english(store, monkeypatch):
    monkeypatch.setattr(mail_router, "get_source", lambda: _Source(False))
    _fail(6, FULL_DISK_ACCESS_HINT)
    st = mail_router.sync_state()
    assert st["state"] == "failing"
    assert st["error_key"] == "fdaNeeded"
    assert st["consecutive_failures"] == 6


def test_once_access_works_the_old_failures_stop_being_reported(store, monkeypatch):
    monkeypatch.setattr(mail_router, "get_source", lambda: _Source(True))
    _fail(6, "PermissionError: [Errno 1] Operation not permitted: '~/Library/Mail'")
    assert mail_router.sync_state()["state"] == "recovering"


def test_other_failures_are_still_failures(store, monkeypatch):
    monkeypatch.setattr(mail_router, "get_source", lambda: _Source(True))
    _fail(2, "SourceError: the mail store is corrupt")
    st = mail_router.sync_state()
    assert st["state"] == "failing" and st["error_key"] is None


def test_a_success_clears_it(store, monkeypatch):
    monkeypatch.setattr(mail_router, "get_source", lambda: _Source(True))
    _fail(3, FULL_DISK_ACCESS_HINT)
    db.record_sync("user", db.now_iso(), ok=True)
    st = mail_router.sync_state()
    assert st["state"] in ("ok", "frozen") and st["error_key"] is None
