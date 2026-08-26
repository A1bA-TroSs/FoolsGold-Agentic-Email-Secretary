"""Local SQLite store. Single-user, single-process; WAL keeps the polling
sync and the request handlers from tripping over each other."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import DATA_DIR, DB_PATH, DEFAULT_SETTINGS, SECRET_SETTINGS
from .crypto import decrypt, encrypt

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id                TEXT PRIMARY KEY,
    conversation_id   TEXT,
    subject           TEXT,
    from_name         TEXT,
    from_address      TEXT,
    to_recipients     TEXT,
    cc_recipients     TEXT,
    received_at       TEXT,
    is_read           INTEGER DEFAULT 0,
    is_answered       INTEGER DEFAULT 0,
    is_flagged        INTEGER DEFAULT 0,
    has_attachments   INTEGER DEFAULT 0,
    importance        TEXT,
    web_link          TEXT,
    folder            TEXT,
    body_preview      TEXT,
    body_text         TEXT,
    body_html         TEXT,
    synced_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_emails_received ON emails(received_at DESC);

CREATE TABLE IF NOT EXISTS priorities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic       TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL DEFAULT 'active',
    weight      INTEGER NOT NULL DEFAULT 10,
    source      TEXT NOT NULL DEFAULT 'user',
    note        TEXT,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS classifications (
    email_id    TEXT PRIMARY KEY,
    bucket      TEXT NOT NULL,
    deadline    TEXT,
    rationale   TEXT,
    score       REAL DEFAULT 0,
    matched     TEXT,
    model       TEXT,
    source      TEXT NOT NULL DEFAULT 'structural',
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_class_score ON classifications(score DESC);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    secret     BLOB,
    encrypted  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    account     TEXT,
    cache_blob  BLOB,
    updated_at  TEXT
);

-- What the user told us about a specific email, by acting on it in the UI.
-- This is the only ranking input that is unambiguously ground truth, so it
-- outweighs every inferred signal.
CREATE TABLE IF NOT EXISTS feedback (
    email_id    TEXT PRIMARY KEY,
    verdict     TEXT NOT NULL,          -- done | pinned | not_important | snoozed
    snooze_until TEXT,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS digests (
    day        TEXT PRIMARY KEY,
    body       TEXT,
    model      TEXT,
    created_at TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT
# EXISTS", and CREATE TABLE IF NOT EXISTS silently leaves an existing table
# alone -- so a schema change has to be applied by hand or upgrading users get
# "no such column" instead of the feature.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("emails", "is_answered", "INTEGER DEFAULT 0"),
    ("emails", "is_flagged", "INTEGER DEFAULT 0"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue          # table not created yet; SCHEMA will include the column
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value, encrypted) VALUES (?, ?, 0)",
                (key, value),
            )
        conn.commit()


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value, secret, encrypted FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return DEFAULT_SETTINGS.get(key, default)
    if row["encrypted"]:
        return decrypt(row["secret"])
    return row["value"] if row["value"] is not None else default


def set_setting(key: str, value: str) -> None:
    encrypted = key in SECRET_SETTINGS
    with connect() as conn:
        if encrypted:
            conn.execute(
                "INSERT INTO settings (key, value, secret, encrypted) VALUES (?, NULL, ?, 1) "
                "ON CONFLICT(key) DO UPDATE SET value=NULL, secret=excluded.secret, encrypted=1",
                (key, encrypt(value)),
            )
        else:
            conn.execute(
                "INSERT INTO settings (key, value, secret, encrypted) VALUES (?, ?, NULL, 0) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, secret=NULL, encrypted=0",
                (key, value),
            )
        conn.commit()


def all_settings(redact_secrets: bool = True) -> dict[str, Any]:
    """Secret values never leave the backend as plaintext; the UI only needs to
    know whether a key is present so it can show 'configured' vs 'not set'."""
    out: dict[str, Any] = dict(DEFAULT_SETTINGS)
    with connect() as conn:
        for row in conn.execute("SELECT key, value, secret, encrypted FROM settings"):
            if row["encrypted"]:
                out[row["key"]] = "********" if (redact_secrets and row["secret"]) else decrypt(row["secret"])
                out[row["key"] + "_set"] = bool(row["secret"])
            else:
                out[row["key"]] = row["value"]
    return out


# --------------------------------------------------------------------------
# emails
# --------------------------------------------------------------------------

def upsert_emails(rows: Iterable[dict[str, Any]]) -> int:
    """Insert or refresh cached messages. Body columns are only overwritten when
    the incoming payload actually carries a body, so a metadata-only delta sync
    can never blank out mail we already fetched in full."""
    sql = """
    INSERT INTO emails (
        id, conversation_id, subject, from_name, from_address, to_recipients,
        cc_recipients, received_at, is_read, is_answered, is_flagged,
        has_attachments, importance,
        web_link, folder, body_preview, body_text, body_html, synced_at
    ) VALUES (
        :id, :conversation_id, :subject, :from_name, :from_address, :to_recipients,
        :cc_recipients, :received_at, :is_read, :is_answered, :is_flagged,
        :has_attachments, :importance,
        :web_link, :folder, :body_preview, :body_text, :body_html, :synced_at
    )
    ON CONFLICT(id) DO UPDATE SET
        subject=excluded.subject,
        is_read=excluded.is_read,
        is_answered=excluded.is_answered,
        is_flagged=excluded.is_flagged,
        has_attachments=excluded.has_attachments,
        importance=excluded.importance,
        folder=excluded.folder,
        body_preview=excluded.body_preview,
        body_text=COALESCE(NULLIF(excluded.body_text, ''), emails.body_text),
        body_html=COALESCE(NULLIF(excluded.body_html, ''), emails.body_html),
        synced_at=excluded.synced_at
    """
    count = 0
    with connect() as conn:
        for row in rows:
            conn.execute(sql, row)
            count += 1
        conn.commit()
    return count


def unclassified_email_ids(limit: int = 100) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT e.id FROM emails e LEFT JOIN classifications c ON c.email_id = e.id "
            "WHERE c.email_id IS NULL ORDER BY e.received_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [r["id"] for r in rows]


def get_emails(ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM emails WHERE id IN ({placeholders})", ids).fetchall()
    return [dict(r) for r in rows]


def save_classification(rec: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO classifications (email_id, bucket, deadline, rationale, score, matched, model, source, created_at) "
            "VALUES (:email_id, :bucket, :deadline, :rationale, :score, :matched, :model, :source, :created_at) "
            "ON CONFLICT(email_id) DO UPDATE SET bucket=excluded.bucket, deadline=excluded.deadline, "
            "rationale=excluded.rationale, score=excluded.score, matched=excluded.matched, "
            "model=excluded.model, source=excluded.source, created_at=excluded.created_at",
            rec,
        )
        conn.commit()


# --------------------------------------------------------------------------
# feedback
# --------------------------------------------------------------------------

VALID_VERDICTS = {"done", "pinned", "not_important", "snoozed"}


def set_feedback(email_id: str, verdict: str, snooze_until: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO feedback (email_id, verdict, snooze_until, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(email_id) DO UPDATE SET "
            "verdict=excluded.verdict, snooze_until=excluded.snooze_until, "
            "created_at=excluded.created_at",
            (email_id, verdict, snooze_until, now_iso()),
        )
        conn.commit()


def clear_feedback(email_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM feedback WHERE email_id = ?", (email_id,))
        conn.commit()


def all_feedback() -> dict[str, dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM feedback").fetchall()
    return {r["email_id"]: dict(r) for r in rows}


def json_list(value: Any) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return []
