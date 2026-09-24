"""A structural briefing is not rebuilt on every read.

Regression (2026-09-21): with a local model configured but not answering, every
GET of the briefing threw away the cached structural one and tried the model
again. Ticking a row re-reads the briefing to drop it and pull up the next --
so the ticked row sat there, struck through, waiting on a model call that was
going to fail the same way.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from app import pipeline


def _db(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    db.init_db()
    return db


def _store(db, model, created):
    body = json.dumps({"headline": {"key": "nothingWaiting"}, "items": [],
                       "logic_version": pipeline.DIGEST_LOGIC_VERSION})
    with db.connect() as conn:
        conn.execute("INSERT INTO digests (day, body, model, created_at) VALUES (?,?,?,?)",
                     (date.today().isoformat(), body, model, created.isoformat()))
        conn.commit()


def test_a_fresh_fallback_is_served_not_rebuilt(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    db.set_setting("llm_provider", "ollama")
    _store(db, "structural", datetime.now(timezone.utc))
    assert pipeline.cached_digest() is not None


def test_a_stale_fallback_tries_the_model_again(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    db.set_setting("llm_provider", "ollama")
    old = datetime.now(timezone.utc) - timedelta(minutes=pipeline.DIGEST_AI_RETRY_MINUTES + 1)
    _store(db, "structural", old)
    assert pipeline.cached_digest() is None


def test_with_ai_off_a_structural_briefing_is_simply_the_answer(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch)
    db.set_setting("llm_provider", "none")
    _store(db, "structural", datetime.now(timezone.utc) - timedelta(hours=5))
    assert pipeline.cached_digest() is not None
