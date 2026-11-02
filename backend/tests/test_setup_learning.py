"""F4: a new install learns from its first click; an existing one is left alone.

The epoch was manual to keep weeks of QA clicking out of the model. A database
created just now has no such history, so it starts learning at creation.
Any database that existed before keeps the manual switch."""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db, learning


def point(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")


def old_db(tmp_path, rows):
    con = sqlite3.connect(tmp_path / "t.db")
    con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, secret BLOB, "
                "encrypted INTEGER NOT NULL DEFAULT 0)")
    con.executemany("INSERT INTO settings (key, value, encrypted) VALUES (?, ?, 0)", rows)
    con.commit()
    con.close()


def test_new_database_learns_from_creation(tmp_path, monkeypatch):
    point(tmp_path, monkeypatch)
    db.init_db()
    stamp = learning.epoch_start()
    assert stamp
    db.init_db()
    assert learning.epoch_start() == stamp


@pytest.mark.parametrize("rows", [
    [("user_address", "you@x.com"), ("learning_epoch_start", "")],   # Danny's QA install
    [("llm_provider", "copilot")],                                     # an older version, no epoch row
])
def test_existing_database_keeps_learning_off(tmp_path, monkeypatch, rows):
    point(tmp_path, monkeypatch)
    old_db(tmp_path, rows)
    db.init_db()
    db.init_db()
    assert learning.epoch_start() == ""


def test_existing_epoch_is_never_moved(tmp_path, monkeypatch):
    point(tmp_path, monkeypatch)
    old_db(tmp_path, [("learning_epoch_start", "2026-10-01T00:00:00+00:00")])
    db.init_db()
    assert learning.epoch_start() == "2026-10-01T00:00:00+00:00"


def test_manual_switch_still_works_on_an_existing_install(tmp_path, monkeypatch):
    point(tmp_path, monkeypatch)
    old_db(tmp_path, [("user_address", "you@x.com")])
    db.init_db()
    stamp = learning.begin_epoch()
    assert learning.epoch_start() == stamp


def test_feedback_after_a_fresh_install_is_within_the_epoch(tmp_path, monkeypatch):
    from app import relevance
    point(tmp_path, monkeypatch)
    db.init_db()
    assert relevance.within_epoch(db.now_iso(), learning.epoch_start())


def test_setup_priority_is_saved_as_an_active_priority(tmp_path, monkeypatch):
    from app import priority
    from app.main import app
    point(tmp_path, monkeypatch)
    db.init_db()
    with TestClient(app) as client:
        r = client.post("/api/priorities", json={"topic": "COMP4332 final project", "status": "active", "weight": 10})
        assert r.status_code == 200, r.text
        assert client.get("/api/ranking").json()["learning"]["enabled"] is True
    assert priority.priority_topics() == ["COMP4332 final project"]


def test_emptied_settings_table_is_not_a_new_install(tmp_path, monkeypatch):
    point(tmp_path, monkeypatch)
    old_db(tmp_path, [])
    db.init_db()
    assert learning.epoch_start() == ""


def test_priority_question_flag_is_a_known_setting(tmp_path, monkeypatch):
    from app.main import app
    point(tmp_path, monkeypatch)
    db.init_db()
    assert db.get_setting("setup_priority_asked") == ""
    with TestClient(app) as client:
        r = client.patch("/api/settings", json={"values": {"setup_priority_asked": "1"}})
    assert r.status_code == 200, r.text
    assert db.get_setting("setup_priority_asked") == "1"
