"""A database from an older version must work, whoever opens it.

The failure this pins is exact and was reported from a real machine:

    .venv/bin/python scripts/build_eval_set.py --n 120
    sqlite3.OperationalError: no such table: eval_labels

`init_db()` ran in exactly one place, the FastAPI lifespan hook. So the schema
was current if and only if the web app had started -- and a standalone script
has no lifespan hook. The same trap was set for `sync_events`, one layer worse,
because `GET /api/mail` reads it: an older database would have answered the
mail list with a 500 until someone restarted the backend.

These build genuinely legacy files with raw sqlite3 -- not `db.connect()`,
which is now the thing that fixes them -- and then use the app normally.
"""
from __future__ import annotations

import sqlite3

import pytest

from app import db

# Tables added after the first release. Each one is a chance to ship exactly
# this bug again.
LATE_TABLES = ("eval_labels", "sync_events", "ranking_weights",
               "learning_events", "priority_centroids")


def _legacy_db(path):
    """A file that exists and has some of the schema, as an upgrader's would."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, "
                 "value TEXT, secret TEXT, encrypted INTEGER DEFAULT 0)")
    conn.commit()
    conn.close()


@pytest.fixture()
def legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "old.db")
    # This process may already have initialised a database at some other path;
    # the cache is keyed on the path, so a new path is a new decision.
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    _legacy_db(tmp_path / "old.db")
    return tmp_path


@pytest.mark.parametrize("table", LATE_TABLES)
def test_opening_an_old_database_brings_every_late_table_with_it(legacy, table):
    with db.connect() as conn:
        found = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
    assert found, f"{table} is missing from a database opened by db.connect()"


def test_the_eval_scripts_query_that_failed_on_a_real_machine(legacy):
    """The exact statement from build_eval_set.py line 77."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT email_id FROM eval_labels WHERE bucket IS NOT NULL").fetchall()
    assert rows == []


def test_the_mail_list_does_not_500_on_an_old_database(legacy):
    """`sync_state()` reads a table that did not exist last week, and it is on
    the path of the app's busiest endpoint."""
    from app.routers.mail import sync_state
    state = sync_state()
    assert state["state"] == "unknown", state


def test_the_schema_is_brought_up_once_per_database_not_once_per_connection(legacy):
    """`init_db()` runs migrations and a duplicate-task collapse. Doing that on
    every connection would put it in the middle of every query in the app."""
    calls = []
    real = db.init_db
    try:
        db.init_db = lambda: (calls.append(1), real())[1]   # noqa: E731
        for _ in range(5):
            with db.connect() as conn:
                conn.execute("SELECT 1").fetchone()
    finally:
        db.init_db = real
    assert len(calls) == 1, f"init_db ran {len(calls)} times for one database"


def test_a_second_database_in_the_same_process_is_also_initialised(tmp_path, monkeypatch):
    """The cache is keyed on the path. A bare boolean would leave every
    database after the first one on whatever schema it happened to have --
    which is how the tests themselves would have started lying."""
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    for name in ("first.db", "second.db"):
        monkeypatch.setattr(db, "DATA_DIR", tmp_path)
        monkeypatch.setattr(db, "DB_PATH", tmp_path / name)
        _legacy_db(tmp_path / name)
        with db.connect() as conn:
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE name='eval_labels'").fetchone(), name
