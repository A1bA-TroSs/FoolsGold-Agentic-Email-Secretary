"""Local SQLite store. Single-user, single-process; WAL keeps the polling
sync and the request handlers from tripping over each other."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .config import DATA_DIR, DB_PATH, DEFAULT_SETTINGS, SECRET_SETTINGS
from .crypto import decrypt, encrypt

SCHEMA = """
-- Permission to send mail to a cloud AI provider (Apple guideline 5.1.2(i),
-- Korea PIPA art. 17/28-8). A ledger, not a flag: each grant and each
-- withdrawal is a row, so "when did I agree, to what, and to whom" has an
-- answer. A grant counts only for the recipient and disclosure version it was
-- given for -- see app/consent.py.
CREATE TABLE IF NOT EXISTS ai_consent (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider      TEXT NOT NULL,
    recipient     TEXT NOT NULL,
    version       INTEGER NOT NULL,
    granted_at    TEXT NOT NULL,
    withdrawn_at  TEXT
);

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
    email_id     TEXT,                     -- the first message this was read out of
    origin       TEXT NOT NULL DEFAULT 'user',  -- user | email
    dedup_key    TEXT                      -- normalised title + day; see dedup_key()
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

-- Which messages named a given to-do. A commitment is a thing in the world, not
-- a property of one email: the same deadline routinely arrives twice -- the
-- department's copy and the one you forwarded to yourself -- and holding that
-- link on the task itself made two rows, two chips, one day. So the task is one
-- row and the messages naming it are many.
--
-- Dropping a task because the mail no longer names it now has to ask whether
-- anything ELSE still names it, which is what this table is here to answer.
-- Keyed by what the mail said, not by the row it landed in, so re-reading a
-- message is a no-op even after the user has renamed the item or moved it to
-- another day. Matching on the row's current wording instead would read their
-- edit as a commitment we no longer hold, delete it, and file the model's
-- original phrasing back in its place.
CREATE TABLE IF NOT EXISTS task_sources (
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    email_id   TEXT NOT NULL,
    source_key TEXT NOT NULL,        -- the commitment as this email worded it
    created_at TEXT,
    PRIMARY KEY (email_id, source_key)
);
CREATE INDEX IF NOT EXISTS idx_task_sources_task ON task_sources(task_id);

-- Messages this app has built, and what became of them.
--
-- The point is not storage, it is the approval gate. The send endpoint takes a
-- token and nothing else, so the only thing that can be sent is a message the
-- server already rendered and handed to the user to look at. There is no field
-- on the send request that can change a recipient, a subject or a word of the
-- body -- not because the handler is careful, but because it has nowhere to
-- put one.
--
-- `sent_at` also makes sending idempotent: a second click on a token that has
-- already gone returns what happened the first time instead of sending twice.
--
-- And it is the log the drafting model will need later: what was shown, what
-- was sent, and the distance between them is the only label-free quality
-- signal available for a single user.
CREATE TABLE IF NOT EXISTS outgoing (
    token       TEXT PRIMARY KEY,
    email_id    TEXT,                  -- the parent, for a reply or forward
    action      TEXT NOT NULL,         -- new | reply | reply_all | forward
    mime        BLOB NOT NULL,         -- exactly what will be transmitted
    summary     TEXT NOT NULL,         -- JSON, for re-rendering the preview
    created_at  TEXT NOT NULL,
    sent_at     TEXT,
    outcome     TEXT                   -- JSON SendResult
);
CREATE INDEX IF NOT EXISTS idx_outgoing_created ON outgoing(created_at);

CREATE TABLE IF NOT EXISTS digests (
    day        TEXT PRIMARY KEY,
    body       TEXT,
    model      TEXT,
    created_at TEXT
);

-- Every sync run, with its outcome. Writeback as audit trail, applied to the
-- one operation the user experiences as "is this thing working".
--
-- The background poller swallowed every exception on purpose -- a flaky
-- network must not take down the app someone is reading mail in -- and the
-- cost of that was a mailbox frozen on 14 September with nothing anywhere
-- saying why. "Nothing new arrived" and "this has been broken for five days"
-- produced identical screens.
--
-- `newest_received` is the freshness fact that matters and is not derivable
-- from the counts: a sync can succeed, write fifty rows, and still be looking
-- at a mail store that stopped updating days ago, because this app reads Apple
-- Mail's files rather than the mail server. That distinction is invisible
-- without this column.
CREATE TABLE IF NOT EXISTS sync_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    trigger         TEXT NOT NULL,          -- poller | user | startup
    ok              INTEGER NOT NULL DEFAULT 0,
    error           TEXT NOT NULL DEFAULT '',
    scanned         INTEGER NOT NULL DEFAULT 0,
    fetched         INTEGER NOT NULL DEFAULT 0,
    written         INTEGER NOT NULL DEFAULT 0,
    newest_received TEXT                    -- the newest received_at now in the db
);

-- The evaluation set: the user's own mail, labelled by hand, kept in the user's
-- own database and nowhere else.
--
-- There is no published Korean benchmark for any of the models this app can
-- run -- I looked, and the gap is real -- so the only ground truth available is
-- this mailbox. That makes the labels the most valuable artifact in the
-- project, and also the most sensitive: they are a judgement about real mail
-- from real people. They live here, beside the mail they describe, and no
-- script in this repo writes them anywhere else.
--
-- `bucket IS NULL` means sampled but not yet labelled, which is what lets the
-- labeller be resumed across sittings instead of demanding one long one.
CREATE TABLE IF NOT EXISTS eval_labels (
    email_id     TEXT PRIMARY KEY,
    bucket       TEXT,                    -- action | fyi | noise, NULL = unlabelled
    deadline     TEXT,                    -- YYYY-MM-DD, or NULL for "none in the text"
    tasks        TEXT NOT NULL DEFAULT '[]',   -- JSON list of {title, due}
    addressed    INTEGER,                 -- 1 if it is addressed to the user personally
    language     TEXT,                    -- ko | en | mixed, assigned at sampling
    stratum      TEXT,                    -- how it was chosen, so coverage is auditable
    note         TEXT NOT NULL DEFAULT '',
    labelled_at  TEXT
);

-- The learned half of relevance. Deliberately a table of scalars rather than a
-- blob in `settings`: every weight is inspectable, one row at a time, and the
-- thing that changed is legible in a diff.
CREATE TABLE IF NOT EXISTS ranking_weights (
    name       TEXT PRIMARY KEY,
    value      REAL NOT NULL DEFAULT 0,
    updated_at TEXT
);

-- Writeback as audit trail: current state and the record of how it got there
-- are separate artifacts. Every signal offered to the model lands here --
-- INCLUDING the refusals, with their reason. A refusal is information; a
-- system that silently drops what it will not learn from cannot be asked why
-- it failed to learn.
CREATE TABLE IF NOT EXISTS learning_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id   TEXT,
    kind       TEXT NOT NULL,
    target     REAL,
    applied    INTEGER NOT NULL DEFAULT 0,
    reason     TEXT NOT NULL,
    confidence REAL DEFAULT 0,
    error      REAL DEFAULT 0,
    deltas     TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_learning_created ON learning_events(created_at DESC);

-- One centroid per priority topic, drifting under Rocchio. `vector` is a JSON
-- array; the embedder that produced it is named so a change of model does not
-- silently compare vectors from two different spaces.
CREATE TABLE IF NOT EXISTS priority_centroids (
    topic      TEXT PRIMARY KEY,
    vector     TEXT NOT NULL,
    embedder   TEXT NOT NULL DEFAULT 'hashing',
    updated_at TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Databases this process has already brought up to schema. Keyed on the path,
# not a bare boolean, because the tests repoint DB_PATH per test and a global
# flag would leave every database after the first one uninitialised.
_SCHEMA_READY: set[str] = set()


def connect() -> sqlite3.Connection:
    """A connection to a database that is guaranteed to have the current schema.

    The guarantee is the point. `init_db()` used to run in exactly one place --
    the FastAPI lifespan hook -- so any code path that did not start the web app
    was working against whatever schema happened to be on disk. That is not
    hypothetical: `scripts/build_eval_set.py` died with `no such table:
    eval_labels` on a database created before that table existed, because a
    standalone script has no lifespan hook to run.

    The same trap was set for `sync_events`, one layer deeper and worse: it is
    read by `GET /api/mail`, so an older database would have answered the mail
    list with a 500 until someone restarted the backend.

    Both are the shape this project keeps re-learning -- a rule that holds only
    while everybody remembers to follow it. Making the connection itself
    responsible removes the remembering. `init_db()` is idempotent (every
    statement is IF NOT EXISTS, every migration checks first), and this runs at
    most once per database per process.
    """
    global _SCHEMA_READY
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    key = str(DB_PATH)
    if key not in _SCHEMA_READY:
        # Added BEFORE the call, because init_db() calls connect() and would
        # otherwise recurse until the stack ran out.
        _SCHEMA_READY.add(key)
        try:
            init_db()
        except Exception:
            _SCHEMA_READY.discard(key)
            conn.close()
            raise
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
    ("tasks", "dedup_key", "TEXT"),
    # Two axes recorded alongside the bucket they produced, so a ranking can be
    # re-derived and argued with rather than merely trusted.
    ("classifications", "actionability", "REAL"),
    ("classifications", "relevance", "REAL"),
    ("classifications", "explored", "INTEGER DEFAULT 0"),
    # What the provider said, before the two axes re-derived the bucket. Kept so
    # that re-deriving never feeds on its own previous output.
    # How many signals have touched this feature. Not bookkeeping: the step
    # size is scaled by it, so a feature the model has never seen moves at full
    # speed on its first correction and a well-estimated one settles down.
    ("ranking_weights", "count", "INTEGER NOT NULL DEFAULT 0"),
    # What KIND of thing this is, from a fixed eight-way taxonomy. Stored so a
    # per-category preference can be learned and, more importantly, inspected:
    # "you have marked six of eight hackathon invitations irrelevant" is a
    # sentence the user can agree or disagree with. A hidden weight is not.
    # Where this message came from on disk, so an inline image can be read out
    # of the original file on demand instead of being copied into the database.
    # 543 of 1,274 messages in one real mailbox carry `cid:` images, 1,405
    # references in all -- storing those bytes here would have roughly doubled
    # the database to serve pictures nobody has asked to see yet.
    ("emails", "source_path", "TEXT"),
    ("classifications", "category", "TEXT"),
    ("classifications", "model_bucket", "TEXT"),
    # Why this email ranks where it does, as a code the UI translates. Stored
    # rather than computed on read so the list does not need one API call per
    # row, and so the explanation is the same object the score was.
    ("classifications", "reason_code", "TEXT"),
    ("classifications", "reason_arg", "TEXT"),
    # How a signal arrived, not just that it did. This is what separates a QA
    # click from a triage decision -- the rows were identical before.
    ("feedback", "provenance", "TEXT"),
    ("feedback", "dwell_ms", "INTEGER DEFAULT 0"),
    ("feedback", "burst_index", "INTEGER DEFAULT 0"),
]

# Indexes added after the first release. CREATE INDEX IF NOT EXISTS is safe to
# re-run, but it cannot run before _migrate() has added the columns it names.
LATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_tasks_email ON tasks(email_id)",
    # Superseded by idx_tasks_dedup below. It was scoped per email -- unique on
    # (email_id, title, due_date) -- which is precisely why two messages naming
    # the same deadline each got a row of their own.
    "DROP INDEX IF EXISTS idx_tasks_from_email",
    # One commitment, one row, whichever mail carried it. Restricted to
    # extracted rows: two hand-written items that happen to read alike are the
    # user's business and must not fail their insert.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_dedup "
    "ON tasks(dedup_key) WHERE origin = 'email' AND dedup_key IS NOT NULL",
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in MIGRATIONS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue          # table not created yet; SCHEMA will include the column
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


class DuplicateTask(ValueError):
    """An edit would have made two rows the same commitment."""


def dedup_key(title: str, due_date: str) -> str:
    """What makes two to-dos the same to-do: one job, one day.

    Two departments describing one deadline rarely type it identically -- a
    trailing full stop, a doubled space, a capital where the other used lower
    case -- so the comparison is on the normalised text, not the stored title.
    The stored title stays exactly as the model wrote it; this is only the key."""
    text = re.sub(r"\s+", " ", (title or "")).strip().casefold()
    text = text.rstrip(" .!?。")
    return f"{(due_date or '').strip()}|{text}"


def _collapse_duplicate_tasks(conn: sqlite3.Connection) -> None:
    """One-time repair for databases written before a to-do was a thing rather
    than a property of an email.

    This has to run before the unique index is created, or the index creation
    fails on the very rows it exists to prevent. It is idempotent: on a database
    with nothing to collapse it fills in the keys and does no writes.

    Merging two halves of one commitment must not lose what the user said about
    either half. A tick survives -- they did the thing, and which chip they
    ticked is an accident of which one they happened to see. A removal does not
    survive a surviving twin: the row stays visible if any copy was visible,
    because bringing something back is a click and noticing it never came back
    is not."""
    # The index this prepares the ground for is rebuilt straight afterwards, by
    # LATE_INDEXES. It has to come off first: on the second and every later
    # start it already exists, and filling in a key would then trip the very
    # constraint this function is here to make satisfiable.
    conn.execute("DROP INDEX IF EXISTS idx_tasks_dedup")

    rows = conn.execute(
        "SELECT id, title, due_date, status, created_at, completed_at, deleted_at, "
        "       origin, email_id, dedup_key "
        "FROM tasks ORDER BY id"
    ).fetchall()
    if not rows:
        return

    for row in rows:
        key = dedup_key(row["title"], row["due_date"])
        if row["dedup_key"] != key:
            conn.execute("UPDATE tasks SET dedup_key = ? WHERE id = ?", (key, row["id"]))

    # Backfill the link table from the column it replaces, so an upgraded
    # database knows which mail its existing to-dos came out of.
    conn.executemany(
        "INSERT OR IGNORE INTO task_sources (task_id, email_id, source_key, created_at) "
        "VALUES (?, ?, ?, ?)",
        [(r["id"], r["email_id"], dedup_key(r["title"], r["due_date"]), r["created_at"])
         for r in rows if r["email_id"]],
    )

    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if row["origin"] != "email":
            continue          # hand-written items are never merged into anything
        groups.setdefault(dedup_key(row["title"], row["due_date"]), []).append(row)

    for members in groups.values():
        if len(members) < 2:
            continue
        # Visible beats removed; done beats open; oldest breaks the tie.
        survivor = sorted(
            members,
            key=lambda r: (r["deleted_at"] is not None, r["status"] != "done", r["id"]),
        )[0]
        done = next((r for r in members if r["status"] == "done"), None)
        if done is not None and survivor["status"] != "done":
            conn.execute(
                "UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?",
                (done["completed_at"] or now_iso(), survivor["id"]),
            )
        for row in members:
            if row["id"] == survivor["id"]:
                continue
            conn.execute(
                "UPDATE OR IGNORE task_sources SET task_id = ? WHERE task_id = ?",
                (survivor["id"], row["id"]),
            )
            conn.execute("DELETE FROM tasks WHERE id = ?", (row["id"],))


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _collapse_duplicate_tasks(conn)
        for statement in LATE_INDEXES:
            conn.execute(statement)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value, encrypted) VALUES (?, ?, 0)",
                (key, value),
            )
        conn.commit()


# --------------------------------------------------------------------------
# outgoing mail
# --------------------------------------------------------------------------

# A draft the user never sent is not worth keeping for ever, and a table that
# only grows is how a local database becomes a surprise. Anything unsent and
# older than this is cleared when a new draft is made.
DRAFT_TTL_HOURS = 72


def save_draft(token: str, *, action: str, email_id: str | None, mime: bytes,
               summary: dict[str, Any]) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=DRAFT_TTL_HOURS)).isoformat()
    with connect() as conn:
        conn.execute("DELETE FROM outgoing WHERE sent_at IS NULL AND created_at < ?", (cutoff,))
        conn.execute(
            "INSERT OR REPLACE INTO outgoing (token, email_id, action, mime, summary, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (token, email_id, action, mime, json.dumps(summary, ensure_ascii=False), now_iso()))
        conn.commit()


def get_draft(token: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM outgoing WHERE token = ?", (token,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["summary"] = json.loads(out["summary"] or "{}")
    out["outcome"] = json.loads(out["outcome"]) if out["outcome"] else None
    return out


def mark_draft_sent(token: str, outcome: dict[str, Any], sent_at: str = "") -> None:
    """`sent_at` comes from the transport, not from this clock.

    Otherwise the first response reports when the transport acted and every
    later one reports when the row was written, and the same send appears to
    have happened at two different times depending on how you ask.
    """
    with connect() as conn:
        conn.execute("UPDATE outgoing SET sent_at = ?, outcome = ? WHERE token = ?",
                     (sent_at or now_iso(), json.dumps(outcome, ensure_ascii=False), token))
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
            # Clearing a key has to actually clear it. Encrypting "" produces a
            # perfectly good Fernet token, so the row stayed non-empty and the
            # UI kept reporting the key as saved while every request failed
            # with "no key set" -- and there was no way to remove one.
            blob = encrypt(value) if (value or "").strip() else None
            conn.execute(
                "INSERT INTO settings (key, value, secret, encrypted) VALUES (?, NULL, ?, 1) "
                "ON CONFLICT(key) DO UPDATE SET value=NULL, secret=excluded.secret, encrypted=1",
                (key, blob),
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

def email_count() -> int:
    """How many messages are stored. Used as a sanity guard before skipping a
    scan: an empty database means the last sync never finished, whatever the
    fingerprint says.

    Restored 2026-09-21: removed by a stale-base overwrite while
    `AppleMailSource.sync` still called it, which would have raised on every
    poll after the first unchanged scan and reported a healthy mailbox as a
    failing sync."""
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0])


def upsert_emails(rows: Iterable[dict[str, Any]]) -> int:
    """Insert or refresh cached messages. Body columns are only overwritten when
    the incoming payload actually carries a body, so a metadata-only delta sync
    can never blank out mail we already fetched in full."""
    sql = """
    INSERT INTO emails (
        id, conversation_id, subject, from_name, from_address, to_recipients,
        cc_recipients, received_at, is_read, is_answered, is_flagged,
        has_attachments, importance,
        web_link, folder, body_preview, body_text, body_html, source_path, synced_at
    ) VALUES (
        :id, :conversation_id, :subject, :from_name, :from_address, :to_recipients,
        :cc_recipients, :received_at, :is_read, :is_answered, :is_flagged,
        :has_attachments, :importance,
        :web_link, :folder, :body_preview, :body_text, :body_html, :source_path,
        :synced_at
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
        -- Same COALESCE rule as the bodies: the Graph source has no file on
        -- disk and sends nothing here, and a metadata-only pass must not blank
        -- out the path the Apple Mail sync recorded.
        source_path=COALESCE(NULLIF(excluded.source_path, ''), emails.source_path),
        synced_at=excluded.synced_at
    """
    count = 0
    with connect() as conn:
        for row in rows:
            # Defaulted here rather than required of every caller: the Graph
            # source has no file on disk, and every test fixture in this repo
            # predates the column. A named-parameter INSERT raises on a missing
            # key, so without this the day's first sync from any other source
            # would fail outright.
            conn.execute(sql, {**row, "source_path": row.get("source_path") or ""})
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


def structural_email_ids(limit: int = 100) -> list[str]:
    """Mail that was only ever ranked structurally, oldest fallback first.

    These are the rows a failed or absent model left behind. They are not
    wrong, just thin -- no semantic bucket, no extracted to-dos -- so once AI
    is working again they are worth asking about, a batch at a time."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT email_id FROM classifications WHERE source = 'structural' "
            "ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()
    return [r["email_id"] for r in rows]


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
            "INSERT INTO classifications (email_id, bucket, deadline, rationale, score, matched, "
            "model, source, created_at, actionability, relevance, explored, model_bucket, "
            "reason_code, reason_arg) "
            "VALUES (:email_id, :bucket, :deadline, :rationale, :score, :matched, :model, :source, "
            ":created_at, :actionability, :relevance, :explored, :model_bucket, "
            ":reason_code, :reason_arg) "
            "ON CONFLICT(email_id) DO UPDATE SET bucket=excluded.bucket, deadline=excluded.deadline, "
            "rationale=excluded.rationale, score=excluded.score, matched=excluded.matched, "
            "model=excluded.model, source=excluded.source, created_at=excluded.created_at, "
            "actionability=excluded.actionability, relevance=excluded.relevance, "
            "explored=excluded.explored, model_bucket=excluded.model_bucket, "
            "reason_code=excluded.reason_code, reason_arg=excluded.reason_arg",
            # The two axes are optional on the way in so an older caller -- or a
            # test that only cares about the bucket -- does not have to know
            # about them. A missing axis is NULL, which is honestly "not
            # computed" rather than a plausible zero.
            {"actionability": None, "relevance": None, "explored": 0,
             "model_bucket": None, "reason_code": None, "reason_arg": None, **rec},
        )
        conn.commit()


# --------------------------------------------------------------------------
# feedback
# --------------------------------------------------------------------------

# `not_relevant` is new and deliberately NOT a rename of `not_important`.
#
# Two reasons. The meanings differ -- "not important" is a judgement about one
# message, "not relevant" is a judgement about a kind of thing, and the second
# is the one that can teach a category weight. And `not_important` already has
# stored rows from the QA period with murky provenance; folding the new gesture
# into it would poison, on day one, the single cleanest negative signal this
# app will ever get.
VALID_VERDICTS = {"done", "pinned", "not_important", "not_relevant", "snoozed"}


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
            "INSERT INTO tasks (title, due_date, note, status, created_at, email_id, origin, dedup_key) "
            "VALUES (?, ?, ?, 'open', ?, ?, ?, ?)",
            (title, due_date, note, now_iso(), email_id, origin, dedup_key(title, due_date)),
        )
        if email_id:
            conn.execute(
                "INSERT OR IGNORE INTO task_sources (task_id, email_id, source_key, created_at) "
                "VALUES (?, ?, ?, ?)",
                (cursor.lastrowid, email_id, dedup_key(title, due_date), now_iso()),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def sync_email_tasks(email_id: str, wanted: list[dict[str, Any]]) -> dict[str, int]:
    """Make the to-dos this email names match what was just read out of it,
    without stepping on anything the user has touched or on another email.

    Re-reading a message has to be a no-op, so this is a reconcile, not an
    insert:

      * a commitment we already hold is left exactly as it is -- including one
        the user has ticked off or removed, which must not come back;
      * a commitment another message also names is adopted, not duplicated.
        The department's copy of a deadline and the one you forwarded yourself
        are one obligation; they used to be two rows on the same day;
      * a commitment that is no longer in this mail loses this mail as a source,
        and is deleted only if no other message still names it and the user
        never acted on it;
      * an item the user typed by hand is adopted as a source too, but never
        deleted here. Whatever the mail stops saying, they wrote that one down.
    """
    by_key = {dedup_key(t["title"], t["due_date"]): t for t in wanted}
    added = dropped = 0
    stamp = now_iso()

    with connect() as conn:
        linked = conn.execute(
            "SELECT s.source_key, t.id, t.status, t.deleted_at, t.origin "
            "FROM task_sources s JOIN tasks t ON t.id = s.task_id WHERE s.email_id = ?",
            (email_id,),
        ).fetchall()

        for row in linked:
            if row["source_key"] in by_key:
                continue
            conn.execute(
                "DELETE FROM task_sources WHERE email_id = ? AND source_key = ?",
                (email_id, row["source_key"]),
            )
            others = conn.execute(
                "SELECT COUNT(*) AS n FROM task_sources WHERE task_id = ?", (row["id"],)
            ).fetchone()["n"]
            if (others == 0 and row["origin"] == "email"
                    and row["status"] == "open" and row["deleted_at"] is None):
                conn.execute("DELETE FROM tasks WHERE id = ?", (row["id"],))
                dropped += 1

        held = {row["source_key"] for row in linked}
        for key, task in by_key.items():
            if key in held:
                continue      # this mail already accounts for this one; leave it alone
            existing = conn.execute(
                "SELECT id FROM tasks WHERE dedup_key = ? ORDER BY id LIMIT 1", (key,)
            ).fetchone()
            if existing:
                task_id = existing["id"]
            else:
                cursor = conn.execute(
                    "INSERT INTO tasks "
                    "(title, due_date, note, status, created_at, email_id, origin, dedup_key) "
                    "VALUES (?, ?, ?, 'open', ?, ?, 'email', ?)",
                    (task["title"], task["due_date"], task.get("note"), stamp, email_id, key),
                )
                task_id = cursor.lastrowid
                added += 1
            conn.execute(
                "INSERT OR IGNORE INTO task_sources (task_id, email_id, source_key, created_at) "
                "VALUES (?, ?, ?, ?)",
                (task_id, email_id, key, stamp),
            )
        conn.commit()
    return {"added": added, "dropped": dropped}


def email_task_ids() -> set[str]:
    """Emails whose body has been read into to-dos. Their subject line no longer
    needs a place on the calendar -- the to-dos say what is actually owed.

    Sourced from the link table rather than from tasks.email_id, for two
    reasons. The second message naming a commitment counts as read even though
    the row belongs to the first -- without that, the duplicate the dedup just
    removed comes straight back as a subject chip on the same day. And a message
    that has stopped naming anything stops counting, which the column, holding
    only whichever email got there first, could not express.

    Deliberately counts removed and completed to-dos as well as open ones. If
    the user deletes "Submit the Co-op progress report" from the 30th and the
    email's own chip then appears on the 30th instead, the deletion has been
    undone in disguise -- same date, vaguer label. Once we have read a message
    into to-dos, their state is the user's answer about that message."""
    with connect() as conn:
        return {
            r["email_id"] for r in conn.execute(
                "SELECT DISTINCT email_id FROM task_sources"
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
    # Renaming an item or moving it to another day changes what counts as the
    # same item. Leaving the key behind would let the old wording claim a match
    # the row no longer has, and the mail that named it would find nothing.
    if any(k in ("title", "due_date") for k, _ in sets):
        current = get_task(task_id, include_deleted=True) or {}
        merged = {**current, **dict(sets)}
        sets.append(("dedup_key", dedup_key(merged.get("title", ""), merged.get("due_date", ""))))
    clause = ", ".join(f"{k} = ?" for k, _ in sets)
    with connect() as conn:
        try:
            cursor = conn.execute(
                f"UPDATE tasks SET {clause} WHERE id = ? AND deleted_at IS NULL",
                (*[v for _, v in sets], task_id),
            )
        except sqlite3.IntegrityError as exc:
            # Edited into an item that already exists. Saying so beats a 500,
            # and beats silently merging two rows the user still sees as two.
            raise DuplicateTask(
                "There is already an item with that name on that day."
            ) from exc
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


# --------------------------------------------------------------------------
# adaptive ranking: learned weights, the audit trail, and topic centroids
# --------------------------------------------------------------------------
# Three tables and a deliberate separation. `ranking_weights` is the current
# state; `learning_events` is the record of how it got there. Keeping them
# apart is what makes the question "why does it think that?" answerable at all
# -- the state alone can only say what it believes, never how it came to.

def ranking_weights() -> dict[str, float]:
    """Learned deviation from the declared priors. Empty on day one, and empty
    is the correct answer then -- the priors ARE the user's own priority list."""
    with connect() as conn:
        rows = conn.execute("SELECT name, value FROM ranking_weights").fetchall()
    return {r["name"]: float(r["value"]) for r in rows}


def weight_counts() -> dict[str, int]:
    """How many signals have touched each feature.

    This is the per-coordinate half of the learning rate. Google's FTRL paper
    argues the case with a coin-flipping analogy: a single global step size
    "decreases for coin i even when it is not being flipped", which is simply
    wrong -- a feature you have barely observed should move further per
    observation than one you have seen a hundred times. They measured an 11.2%
    AucLoss reduction from per-coordinate rates.

    For this app it is the mechanism behind the thing the user actually asked
    for: a handful of clicks visibly changing the ranking. A brand-new category
    has a count of zero, so its first correction moves it the full distance.
    """
    with connect() as conn:
        rows = conn.execute("SELECT name, count FROM ranking_weights").fetchall()
    return {r["name"]: int(r["count"] or 0) for r in rows}


def save_ranking_weights(weights: dict[str, float], touched: Iterable[str] = ()) -> None:
    stamp = now_iso()
    bump = {str(t) for t in touched}
    with connect() as conn:
        for name, value in weights.items():
            conn.execute(
                "INSERT INTO ranking_weights (name, value, updated_at, count) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at, "
                "count=ranking_weights.count + excluded.count",
                (name, float(value), stamp, 1 if name in bump else 0),
            )
        conn.commit()


def reset_ranking_weights() -> None:
    """Back to the declared priors. The undo for a learning run that went wrong
    -- and the reason learning is safe to switch on at all."""
    with connect() as conn:
        conn.execute("DELETE FROM ranking_weights")
        conn.commit()


def record_learning_event(
    email_id: str,
    kind: str,
    target: float,
    applied: bool,
    reason: str,
    confidence: float = 0.0,
    error: float = 0.0,
    deltas: dict[str, float] | None = None,
) -> None:
    """Every signal offered to the model, accepted or refused, with the reason.

    Refusals are the valuable half. "Nothing was learned from the last 40
    actions because they were all pre-epoch" is a diagnosis; silence is not.
    """
    with connect() as conn:
        conn.execute(
            "INSERT INTO learning_events (email_id, kind, target, applied, reason, "
            "confidence, error, deltas, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (email_id, kind, float(target), 1 if applied else 0, reason,
             float(confidence), float(error), json.dumps(deltas or {}), now_iso()),
        )
        conn.commit()


def learning_events(limit: int = 100, applied_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM learning_events"
    if applied_only:
        sql += " WHERE applied = 1"
    sql += " ORDER BY id DESC LIMIT ?"
    with connect() as conn:
        rows = conn.execute(sql, (int(limit),)).fetchall()
    out = []
    for row in rows:
        rec = dict(row)
        try:
            rec["deltas"] = json.loads(rec.get("deltas") or "{}")
        except (ValueError, TypeError):
            rec["deltas"] = {}
        out.append(rec)
    return out


def learning_summary() -> dict[str, Any]:
    """What the learner has and has not been allowed to do, by reason.

    Built for the case where the user asks why nothing is changing. The honest
    answer is usually a reason code with a large count next to it.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT reason, applied, COUNT(*) AS n FROM learning_events GROUP BY reason, applied"
        ).fetchall()
    applied = sum(r["n"] for r in rows if r["applied"])
    refused: dict[str, int] = {}
    for row in rows:
        if not row["applied"]:
            refused[row["reason"]] = refused.get(row["reason"], 0) + row["n"]
    return {
        "applied": applied,
        "refused": sum(refused.values()),
        "refused_by_reason": dict(sorted(refused.items(), key=lambda kv: -kv[1])),
        "weights": ranking_weights(),
    }


def priority_centroids(embedder: str = "hashing") -> dict[str, list[float]]:
    """Centroids only from the embedder that is currently in use.

    Vectors from two different models live in two different spaces, and a
    cosine between them is a number with no meaning. Filtering here is what
    stops a model swap silently producing confident nonsense.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT topic, vector FROM priority_centroids WHERE embedder = ?", (embedder,)
        ).fetchall()
    out: dict[str, list[float]] = {}
    for row in rows:
        try:
            vec = json.loads(row["vector"])
        except (ValueError, TypeError):
            continue
        if isinstance(vec, list) and vec:
            out[row["topic"]] = [float(v) for v in vec]
    return out


def save_priority_centroid(topic: str, vector: list[float], embedder: str = "hashing") -> None:
    topic = (topic or "").strip()
    if not topic or not vector:
        return
    with connect() as conn:
        conn.execute(
            "INSERT INTO priority_centroids (topic, vector, embedder, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(topic) DO UPDATE SET vector=excluded.vector, "
            "embedder=excluded.embedder, updated_at=excluded.updated_at",
            (topic, json.dumps([round(float(v), 8) for v in vector]), embedder, now_iso()),
        )
        conn.commit()


def drop_priority_centroid(topic: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM priority_centroids WHERE topic = ?", ((topic or "").strip(),))
        conn.commit()


# --------------------------------------------------------------------------
# sync events
# --------------------------------------------------------------------------

def record_sync(trigger: str, started_at: str, *, ok: bool, error: str = "",
                scanned: int = 0, fetched: int = 0, written: int = 0) -> None:
    """One row per attempt, successes and failures alike.

    A failure that is only logged to a console nobody reads is a failure the
    user experiences as "the app is stuck" with no way to find out more.
    """
    with connect() as conn:
        newest = conn.execute("SELECT MAX(received_at) FROM emails").fetchone()[0]
        conn.execute(
            "INSERT INTO sync_events (started_at, finished_at, trigger, ok, error, "
            "scanned, fetched, written, newest_received) VALUES (?,?,?,?,?,?,?,?,?)",
            (started_at, now_iso(), trigger, 1 if ok else 0, (error or "")[:500],
             scanned, fetched, written, newest),
        )
        # Bounded on purpose: this is a diagnostic tail, not a history.
        conn.execute("DELETE FROM sync_events WHERE id <= "
                     "(SELECT MAX(id) - 200 FROM sync_events)")
        conn.commit()


def last_sync() -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM sync_events ORDER BY id DESC LIMIT 1").fetchone()
        failing = conn.execute(
            "SELECT COUNT(*) FROM sync_events WHERE id > "
            "COALESCE((SELECT MAX(id) FROM sync_events WHERE ok = 1), 0)").fetchone()[0]
    if row is None:
        return None
    out = dict(row)
    out["ok"] = bool(out["ok"])
    # How long it has been broken, not just that the last one broke. One failed
    # attempt is a blip; forty in a row is the thing the user is looking at.
    out["consecutive_failures"] = failing
    return out


def source_freshness() -> dict[str, Any]:
    """How long the mail source has been producing nothing, and how hard we looked.

    Not a timeout and not a guess. The app reads Apple Mail's files rather than
    the mail server, so it is exactly as fresh as Apple Mail is -- and when
    Mail.app is closed, or has stopped syncing, this app faithfully shows a
    frozen inbox with no way to tell that apart from a quiet week.

    The evidence that distinguishes them is already recorded: a run of
    SUCCESSFUL syncs over which `newest_received` never moved. Two successful
    checks an hour apart finding the same newest message is a quiet hour. Sixty
    of them over five days is a source that has stopped.
    """
    with connect() as conn:
        newest = conn.execute("SELECT MAX(received_at) FROM emails").fetchone()[0]
        if not newest:
            return {"newest_received": None, "frozen_since": None,
                    "frozen_checks": 0, "frozen_hours": 0.0}
        # The earliest successful sync that already saw this same newest message.
        row = conn.execute(
            "SELECT MIN(finished_at) AS since, COUNT(*) AS n FROM sync_events "
            "WHERE ok = 1 AND newest_received = ?", (newest,)).fetchone()
    since, checks = (row["since"], row["n"]) if row else (None, 0)
    hours = 0.0
    if since:
        try:
            delta = datetime.now(timezone.utc) - datetime.fromisoformat(since)
            hours = round(delta.total_seconds() / 3600.0, 1)
        except ValueError:
            hours = 0.0
    return {"newest_received": newest, "frozen_since": since,
            "frozen_checks": checks, "frozen_hours": hours}


def category_evidence() -> list[dict[str, Any]]:
    """Per category: how many, and what the user did with them.

    The point of this is not the model. It is the sentence it lets the UI
    write: "you have marked six of eight competition emails as not relevant".
    That is a claim the user can agree with, disagree with, or correct -- and a
    learned weight on its own is none of those things.

    The interactive-ML literature is blunt about why this matters: in a
    77-participant email-classification study, showing people what the model
    believed and letting them correct it directly cut the labels needed from
    182 to 47 -- a quarter as many -- and produced a classifier that was ~10%
    more accurate (F1 0.85 vs 0.77). The lever behind "a handful of clicks
    changes things" is explanation, not a bigger step size.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT COALESCE(NULLIF(c.category, ''), 'uncategorised') AS category, "
            "       COUNT(*) AS total, "
            "       SUM(CASE WHEN f.verdict = 'not_relevant' THEN 1 ELSE 0 END) AS dismissed, "
            "       SUM(CASE WHEN f.verdict = 'done' THEN 1 ELSE 0 END) AS done, "
            "       SUM(CASE WHEN f.verdict = 'pinned' THEN 1 ELSE 0 END) AS pinned "
            "FROM classifications c LEFT JOIN feedback f ON f.email_id = c.email_id "
            "GROUP BY category ORDER BY total DESC"
        ).fetchall()
    weights = ranking_weights()
    counts = weight_counts()
    out = []
    for row in rows:
        name = row["category"]
        key = f"cat_{name}"
        out.append({
            "category": name,
            "total": int(row["total"]),
            "dismissed": int(row["dismissed"] or 0),
            "done": int(row["done"] or 0),
            "pinned": int(row["pinned"] or 0),
            "weight": round(float(weights.get(key, 0.0)), 4),
            # How settled the weight is. A category touched twice moves fast and
            # should be presented as provisional; one touched forty times has
            # earned its number.
            "signals": int(counts.get(key, 0)),
        })
    return out
