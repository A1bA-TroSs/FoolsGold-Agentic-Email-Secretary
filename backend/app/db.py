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

-- Muting is about a correspondent, not a message. "This one email is not
-- important" is what the feedback table is for; muting says "nothing from this
-- address is worth ranking", which is how people actually think about a
-- newsletter or an automated notifier.
CREATE TABLE IF NOT EXISTS muted_senders (
    address    TEXT PRIMARY KEY,
    created_at TEXT
);

-- Checklist items the user typed in, as opposed to due dates we read out of
-- mail. They share the calendar and the gestures; only the origin differs.
CREATE TABLE IF NOT EXISTS tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT NOT NULL,
    due_date     TEXT NOT NULL,          -- YYYY-MM-DD, a local calendar day
    note         TEXT,
    status       TEXT NOT NULL DEFAULT 'open',   -- open | done
    created_at   TEXT,
    completed_at TEXT,
    deleted_at   TEXT,                     -- set = in the removed box, not gone
    email_id     TEXT,                     -- the message this was read out of
    origin       TEXT NOT NULL DEFAULT 'user'   -- user | email
);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_date);
-- The indexes over email_id live in LATE_INDEXES, not here: this script runs
-- before _migrate(), so on an upgraded database the column does not exist yet
-- and naming it would fail the whole executescript.

-- Email-derived due dates the user removed from the calendar. Deleting a
-- calendar row means "this is not a deadline", which is a different statement
-- from "I have done it" (feedback) and from "silence this sender" (mute), so
-- it gets its own table instead of overloading either.
CREATE TABLE IF NOT EXISTS calendar_hidden (
    email_id   TEXT PRIMARY KEY,
    created_at TEXT
);

-- The opposite of muting, and deliberately its own table rather than a column
-- on muted_senders: they are independent statements. "Never rank this address"
-- and "colour this address so I spot it" are not two values of one setting,
-- and a sender can sensibly be un-muted back into a highlight it still holds.
CREATE TABLE IF NOT EXISTS highlighted_senders (
    address    TEXT PRIMARY KEY,
    color      TEXT NOT NULL,
    created_at TEXT
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
    ("tasks", "deleted_at", "TEXT"),
    ("tasks", "email_id", "TEXT"),
    ("tasks", "origin", "TEXT NOT NULL DEFAULT 'user'"),
]

# Indexes added after the first release. CREATE INDEX IF NOT EXISTS is safe to
# re-run, but it cannot run before _migrate() has added the columns it names.
LATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_tasks_email ON tasks(email_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_from_email "
    "ON tasks(email_id, title, due_date) WHERE email_id IS NOT NULL",
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
        for statement in LATE_INDEXES:
            conn.execute(statement)
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


# --------------------------------------------------------------------------
# muted senders
# --------------------------------------------------------------------------

def mute_sender(address: str) -> None:
    """Silence an address. A highlight it holds is left in place: muting says
    "not now", not "forget my colour", and unmuting brings the colour back."""
    address = (address or "").strip().lower()
    if not address:
        return
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO muted_senders (address, created_at) VALUES (?, ?)",
            (address, now_iso()),
        )
        conn.commit()


def unmute_sender(address: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM muted_senders WHERE address = ?", ((address or "").strip().lower(),))
        conn.commit()


def muted_senders() -> set[str]:
    with connect() as conn:
        return {r["address"] for r in conn.execute("SELECT address FROM muted_senders")}


def muted_sender_rows() -> list[dict[str, Any]]:
    """Each muted address with how much mail it accounts for, so the muted box
    shows the consequence of the decision rather than a bare list."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT m.address, m.created_at, COUNT(e.id) AS message_count, "
            "       MAX(e.received_at) AS last_seen, "
            "       (SELECT from_name FROM emails WHERE from_address = m.address "
            "         AND from_name != '' ORDER BY received_at DESC LIMIT 1) AS display_name "
            "FROM muted_senders m LEFT JOIN emails e ON e.from_address = m.address "
            "GROUP BY m.address ORDER BY message_count DESC, m.address"
        ).fetchall()
    return [dict(r) for r in rows]


def count_from_sender(address: str) -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n FROM emails WHERE from_address = ?",
            ((address or "").strip().lower(),),
        ).fetchone()
    return int(row["n"]) if row else 0


def json_list(value: Any) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return []


# --------------------------------------------------------------------------
# tasks (user-created checklist items)
# --------------------------------------------------------------------------

def add_task(title: str, due_date: str, note: str | None = None,
             email_id: str | None = None, origin: str = "user") -> dict[str, Any]:
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO tasks (title, due_date, note, status, created_at, email_id, origin) "
            "VALUES (?, ?, ?, 'open', ?, ?, ?)",
            (title, due_date, note, now_iso(), email_id, origin),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def sync_email_tasks(email_id: str, wanted: list[dict[str, Any]]) -> dict[str, int]:
    """Make the extracted to-dos for one email match what was just read out of
    it, without stepping on anything the user has touched.

    Re-reading the same message has to be a no-op, so this is a reconcile, not
    an insert:

      * a commitment we already hold is left exactly as it is -- including one
        the user has ticked off or removed, which must not come back;
      * a commitment that is no longer in the mail is dropped, but only if it
        is still open and untouched;
      * anything the user wrote by hand is invisible here (email_id is NULL).
    """
    keys = {(t["title"], t["due_date"]) for t in wanted}
    added = dropped = 0
    with connect() as conn:
        existing = conn.execute(
            "SELECT id, title, due_date, status, deleted_at FROM tasks WHERE email_id = ?",
            (email_id,),
        ).fetchall()

        for row in existing:
            if (row["title"], row["due_date"]) in keys:
                continue
            # Gone from the mail. Only clear it away if the user never acted.
            if row["status"] == "open" and row["deleted_at"] is None:
                conn.execute("DELETE FROM tasks WHERE id = ?", (row["id"],))
                dropped += 1

        for task in wanted:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO tasks "
                "(title, due_date, note, status, created_at, email_id, origin) "
                "VALUES (?, ?, ?, 'open', ?, ?, 'email')",
                (task["title"], task["due_date"], task.get("note"), now_iso(), email_id),
            )
            added += cursor.rowcount
        conn.commit()
    return {"added": added, "dropped": dropped}


def email_task_ids() -> set[str]:
    """Emails whose body has been read into to-dos. Their subject line no longer
    needs a place on the calendar -- the to-dos say what is actually owed.

    Deliberately counts removed and completed to-dos as well as open ones. If
    the user deletes "Submit the Co-op progress report" from the 30th and the
    email's own chip then appears on the 30th instead, the deletion has been
    undone in disguise -- same date, vaguer label. Once we have read a message
    into to-dos, their state is the user's answer about that message."""
    with connect() as conn:
        return {
            r["email_id"] for r in conn.execute(
                "SELECT DISTINCT email_id FROM tasks WHERE email_id IS NOT NULL"
            )
        }


def get_task(task_id: int, include_deleted: bool = False) -> dict[str, Any] | None:
    clause = "" if include_deleted else " AND deleted_at IS NULL"
    with connect() as conn:
        row = conn.execute(f"SELECT * FROM tasks WHERE id = ?{clause}", (task_id,)).fetchone()
    return dict(row) if row else None


def update_task(task_id: int, **fields: Any) -> dict[str, Any] | None:
    """Only the columns actually passed are written. Completing a task stamps
    completed_at; reopening clears it, so a re-ticked item does not keep a
    completion time from a previous life."""
    allowed = ("title", "due_date", "note", "status")
    sets = [(k, v) for k, v in fields.items() if k in allowed and v is not None]
    if "status" in dict(sets):
        status = dict(sets)["status"]
        sets.append(("completed_at", now_iso() if status == "done" else None))
    if not sets:
        return get_task(task_id)
    clause = ", ".join(f"{k} = ?" for k, _ in sets)
    with connect() as conn:
        cursor = conn.execute(
            f"UPDATE tasks SET {clause} WHERE id = ? AND deleted_at IS NULL",
            (*[v for _, v in sets], task_id),
        )
        conn.commit()
    return get_task(task_id) if cursor.rowcount else None


def delete_task(task_id: int) -> bool:
    """Soft delete. A checklist item you wrote is not something we should
    destroy on one click -- it goes to the removed box, where it can come back
    to the day it was filed on."""
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE tasks SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
            (now_iso(), task_id),
        )
        conn.commit()
    return cursor.rowcount > 0


def restore_task(task_id: int) -> bool:
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE tasks SET deleted_at = NULL WHERE id = ? AND deleted_at IS NOT NULL",
            (task_id,),
        )
        conn.commit()
    return cursor.rowcount > 0


def purge_task(task_id: int) -> bool:
    """The one irreversible path, reachable only from the removed box."""
    with connect() as conn:
        cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
    return cursor.rowcount > 0


def removed_tasks() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks t LEFT JOIN emails e ON e.id = t.email_id "
            f"WHERE t.deleted_at IS NOT NULL AND {_NOT_MUTED} ORDER BY t.deleted_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# A task read out of a muted sender's mail is that sender's mail. The LEFT JOIN
# keeps hand-written tasks (email_id NULL), which have no sender to mute.
_TASK_COLUMNS = (
    "t.*, e.from_name AS sender_name, e.from_address AS sender_address, e.subject AS email_subject"
)
_NOT_MUTED = (
    "(t.email_id IS NULL OR e.from_address IS NULL "
    " OR e.from_address NOT IN (SELECT address FROM muted_senders))"
)


def tasks_between(start: str, end: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            f"SELECT {_TASK_COLUMNS} FROM tasks t LEFT JOIN emails e ON e.id = t.email_id "
            f"WHERE t.due_date BETWEEN ? AND ? AND t.deleted_at IS NULL AND {_NOT_MUTED} "
            f"ORDER BY t.due_date, t.id",
            (start, end),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# calendar visibility
# --------------------------------------------------------------------------

def hide_calendar_email(email_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO calendar_hidden (email_id, created_at) VALUES (?, ?)",
            (email_id, now_iso()),
        )
        conn.commit()


def unhide_calendar_email(email_id: str) -> bool:
    with connect() as conn:
        cursor = conn.execute("DELETE FROM calendar_hidden WHERE email_id = ?", (email_id,))
        conn.commit()
    return cursor.rowcount > 0


def hidden_calendar_rows() -> list[dict[str, Any]]:
    """Hidden due dates with enough context to recognise them a week later.

    An INNER JOIN on purpose: if the message itself has aged out of the local
    cache there is nothing to restore, so it should not be offered.

    Muted senders are excluded for the same reason. Restoring an entry whose
    sender is muted would appear to work and change nothing -- the mute filter
    hides it again the moment the calendar reloads. Unmuting brings the whole
    address back, and this row with it."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT h.email_id, h.created_at AS removed_at, e.subject, e.from_name, "
            "       e.from_address, c.deadline "
            "FROM calendar_hidden h "
            "JOIN emails e ON e.id = h.email_id "
            "JOIN classifications c ON c.email_id = h.email_id "
            "WHERE e.from_address NOT IN (SELECT address FROM muted_senders) "
            "ORDER BY h.created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# highlighted senders
# --------------------------------------------------------------------------

# A small fixed palette rather than a colour picker. Six are enough to tell
# apart at a glance, they can be given real names in every UI language, and
# they map to theme tokens so the colours stay legible on a dark background --
# a free hex value chosen against the ivory theme would vanish in the dark one.
HIGHLIGHT_COLORS = ("red", "orange", "yellow", "green", "blue", "purple")


def highlight_sender(address: str, color: str) -> None:
    """Colour every message from an address.

    Highlighting un-mutes, because the two are opposite instructions: you
    cannot both want a sender out of the way and want it to catch your eye.
    Muting does *not* clear a highlight, so unmuting later restores the colour
    the user chose rather than silently losing it."""
    address = (address or "").strip().lower()
    if not address:
        return
    if color not in HIGHLIGHT_COLORS:
        raise ValueError(f"color must be one of {list(HIGHLIGHT_COLORS)}")
    with connect() as conn:
        conn.execute(
            "INSERT INTO highlighted_senders (address, color, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(address) DO UPDATE SET color=excluded.color",
            (address, color, now_iso()),
        )
        conn.execute("DELETE FROM muted_senders WHERE address = ?", (address,))
        conn.commit()


def unhighlight_sender(address: str) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM highlighted_senders WHERE address = ?",
            ((address or "").strip().lower(),),
        )
        conn.commit()


def sender_highlights() -> dict[str, str]:
    """address -> colour, for whoever needs to paint a row."""
    with connect() as conn:
        return {r["address"]: r["color"] for r in conn.execute(
            "SELECT address, color FROM highlighted_senders"
        )}


def highlighted_sender_rows() -> list[dict[str, Any]]:
    """Each highlighted address with how much mail it accounts for, so the box
    shows the same accounting the muted box does."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT h.address, h.color, h.created_at, COUNT(e.id) AS message_count, "
            "       MAX(e.received_at) AS last_seen, "
            "       (SELECT from_name FROM emails WHERE from_address = h.address "
            "         AND from_name != '' ORDER BY received_at DESC LIMIT 1) AS display_name "
            "FROM highlighted_senders h LEFT JOIN emails e ON e.from_address = h.address "
            "GROUP BY h.address, h.color ORDER BY message_count DESC, h.address"
        ).fetchall()
    return [dict(r) for r in rows]
