"""The tester quality check (roadmap A5): sample, label, score, export.

The same evaluation set the terminal scripts build (`scripts/build_eval_set.py`,
`label_eval_set.py`, `eval_extraction.py`), moved where a tester can reach it.
The scripts call into this module, so there is one sampling rule and one floor.

Rules carried over from the scripts, and why they matter for a measurement:
  * The stored classification spreads the sample and is never copied into a
    label: a set seeded by the model measures agreement, not correctness.
  * The labelling screen never sees the app's answer before the label is saved
    (anchoring). `next_item` returns the email and nothing about how it ranked.
  * Every number is per language. An average over a mixed mailbox hides a
    Korean-only regression.
  * The structural floor is the app's own function, never a re-implementation.

Scoring uses the classification already stored, which is what the tester saw,
so a check costs no provider calls.

Apple Mail's Priority mark (Danny, 2026-11-02: read automatically, never asked)
comes from a read-only copy of Mail's `Envelope Index`. That database is not
documented and changes between macOS releases, which is why the mail source
does not use it; here a missing file, table or column only means "not
measured", and nothing else depends on it.
"""
from __future__ import annotations

import random
import re
import shutil
import sqlite3
import tempfile
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import db

BUCKETS = ("action", "fyi", "noise")
DEFAULT_N = 120
DEFAULT_SEED = 20260919

# Hangul syllables, plus Jamo. Decides a stratum, not a label.
_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
_LATIN = re.compile(r"[A-Za-z]")
# 37 of 1,218 real emails carried Hangul, only 8 Korean-dominant: a "which
# script wins" rule reached one Korean email. Hangul content is what matters.
_KO_MIN_CHARS = 8


def language_of(email: dict) -> str:
    text = f"{email.get('subject') or ''} {(email.get('body_text') or '')[:400]}"
    ko, en = len(_HANGUL.findall(text)), len(_LATIN.findall(text))
    if ko > en:
        return "ko"
    if ko >= _KO_MIN_CHARS:
        return "mixed"
    return "en"


# ---------------------------------------------------------------- sampling

def sample(n: int = DEFAULT_N, seed: int = DEFAULT_SEED, reset: bool = False) -> dict[str, Any]:
    """Stratified sample into `eval_labels` (language x bucket x dated),
    round-robin so a small stratum is not drowned. Labelled rows are never
    touched; `reset` drops only unlabelled ones."""
    rng = random.Random(seed)
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT e.id, e.subject, e.body_text, e.received_at, c.bucket, c.deadline "
            "FROM emails e LEFT JOIN classifications c ON c.email_id = e.id")]
        already = {r["email_id"] for r in conn.execute(
            "SELECT email_id FROM eval_labels WHERE bucket IS NOT NULL")}
        if reset:
            conn.execute("DELETE FROM eval_labels WHERE bucket IS NULL")
            conn.commit()
        pending = {r["email_id"] for r in conn.execute(
            "SELECT email_id FROM eval_labels WHERE bucket IS NULL")}

    strata: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        if row["id"] in already or row["id"] in pending:
            continue
        key = (language_of(row), row["bucket"] or "unclassified",
               "dated" if row["deadline"] else "undated")
        strata[key].append(row)

    order = sorted(strata, key=lambda k: (-len(strata[k]), k))
    for key in order:
        rng.shuffle(strata[key])
    picked: list[tuple[dict, tuple]] = []
    i, want = 0, n - len(already) - len(pending)
    while len(picked) < want and any(strata[k] for k in order):
        key = order[i % len(order)]
        if strata[key]:
            picked.append((strata[key].pop(), key))
        i += 1

    with db.connect() as conn:
        for row, key in picked:
            conn.execute("INSERT OR IGNORE INTO eval_labels (email_id, language, stratum) "
                         "VALUES (?, ?, ?)", (row["id"], key[0], "/".join(key)))
        conn.commit()
    return {
        "mailbox": len(rows),
        "already_labelled": len(already),
        "sampled": len(picked),
        "by_language": dict(Counter(k[0] for _, k in picked)),
        "by_stratum": dict(Counter("/".join(k) for _, k in picked)),
        **progress(),
    }


def progress() -> dict[str, int]:
    with db.connect() as conn:
        total, labelled = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN bucket IS NOT NULL THEN 1 ELSE 0 END) "
            "FROM eval_labels").fetchone()
    return {"total": total or 0, "labelled": labelled or 0}


# ---------------------------------------------------------------- labelling

def next_item() -> dict[str, Any] | None:
    """The next unlabelled email: what the tester reads, nothing about how
    the app ranked it."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT e.id, e.subject, e.from_name, e.from_address, e.body_text, e.received_at "
            "FROM eval_labels l JOIN emails e ON e.id = l.email_id "
            "WHERE l.bucket IS NULL ORDER BY l.email_id LIMIT 1").fetchone()
    if row is None:
        return None
    body = (row["body_text"] or "").strip().replace("\r", "")
    return {"email_id": row["id"], "subject": row["subject"] or "",
            "from_name": row["from_name"] or "", "from_address": row["from_address"] or "",
            "received_at": row["received_at"] or "", "body": body[:4000]}


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def save_label(email_id: str, bucket: str, addressed: bool, deadline: str | None) -> None:
    if bucket not in BUCKETS:
        raise ValueError("bucket must be action, fyi or noise")
    deadline = (deadline or "").strip() or None
    if deadline and not _DATE.match(deadline):
        raise ValueError("deadline must be YYYY-MM-DD")
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE eval_labels SET bucket=?, deadline=?, addressed=?, labelled_at=? "
            "WHERE email_id=?",
            (bucket, deadline, 1 if addressed else 0, db.now_iso(), email_id))
        conn.commit()
    if cur.rowcount == 0:
        raise KeyError(email_id)


def undo(email_id: str) -> None:
    """Take back a saved label (a mis-pressed key), so the email is asked again."""
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE eval_labels SET bucket=NULL, deadline=NULL, addressed=NULL, labelled_at=NULL "
            "WHERE email_id=? AND bucket IS NOT NULL", (email_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise KeyError(email_id)


def labelled() -> list[dict]:
    """Labelled rows joined to their email, as the scorers read them."""
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT l.*, e.subject, e.from_name, e.from_address, e.body_text, "
            "       e.body_preview, e.to_recipients, e.cc_recipients, e.received_at, "
            "       e.is_read, e.is_answered, e.is_flagged, e.has_attachments, e.importance, "
            "       e.source_path "
            "FROM eval_labels l JOIN emails e ON e.id = l.email_id "
            "WHERE l.bucket IS NOT NULL")]


# ---------------------------------------------------------- Apple Priority

_EMLX = re.compile(r"^(\d+)(?:\.partial)?\.emlx$")


def _mail_root() -> Path | None:
    """The Apple Mail root, None when there is none. PermissionError (macOS
    privacy protection) propagates so it is reported as no access."""
    from .sources.applemail import find_mail_root
    try:
        return find_mail_root(db.get_setting("applemail_root", "").strip() or None)
    except PermissionError:
        raise
    except OSError:
        return None


def apple_priority(rows: list[dict]) -> tuple[dict[str, bool], str]:
    """Apple Mail's Priority mark per email id, and why it is empty when it is.

    Reads a copy of `MailData/Envelope Index` (and its -wal/-shm), opened
    read-only, so Mail's own file is never opened for writing or locked. The
    `.emlx` file number is `messages.ROWID`; `message_global_data.urgent` is
    the mark (31Carlton7/mac-cli reads the same column for `is:priority`)."""
    try:
        root = _mail_root()
    except PermissionError:
        return {}, "no_access"
    index = root / "MailData" / "Envelope Index" if root else None
    try:
        if index is None or not index.is_file():
            return {}, "no_index"
    except OSError:
        return {}, "no_access"

    numbers: dict[int, str] = {}
    for r in rows:
        m = _EMLX.match(Path(r.get("source_path") or "").name)
        if m:
            numbers[int(m.group(1))] = r["email_id"]
    if not numbers:
        return {}, "no_apple_mail"

    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "Envelope Index"
        try:
            for suffix in ("", "-wal", "-shm"):
                src = Path(f"{index}{suffix}")
                if src.is_file():
                    shutil.copy2(src, f"{copy}{suffix}")
        except OSError:
            return {}, "no_access"
        try:
            con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        except sqlite3.Error:
            return {}, "unreadable"
        try:
            cols = {t: {c[1] for c in con.execute(f"PRAGMA table_info({t})")}
                    for t in ("messages", "message_global_data")}
            if "global_message_id" not in cols["messages"] or "urgent" not in cols["message_global_data"]:
                return {}, "no_priority_column"
            out: dict[str, bool] = {}
            ids = list(numbers)
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                q = ("SELECT m.ROWID, g.urgent FROM messages m "
                     "LEFT JOIN message_global_data g ON g.ROWID = m.global_message_id "
                     f"WHERE m.ROWID IN ({','.join('?' * len(chunk))})")
                for rowid, urgent in con.execute(q, chunk):
                    out[numbers[rowid]] = bool(urgent)
        except sqlite3.Error:
            return {}, "unreadable"
        finally:
            con.close()
    if not out:
        return {}, "no_match"
    return out, ""


# ------------------------------------------------------------------ scoring

def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def score() -> dict[str, Any]:
    """Per-language numbers on the labelled set. Ratios are 0-1, or None when
    there is nothing to divide by."""
    rows = labelled()
    with db.connect() as conn:
        stored = {r["email_id"]: dict(r) for r in conn.execute(
            "SELECT email_id, bucket, deadline, model FROM classifications")}
    from . import pipeline
    me = pipeline.user_address()
    floor = {c.email_id: c for c in pipeline._structural_classifications(
        [dict(r, id=r["email_id"]) for r in rows], me)}
    apple, apple_reason = apple_priority(rows)

    per: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        d = per[r["language"] or "en"]
        want = r["bucket"]
        got = (stored.get(r["email_id"]) or {}).get("bucket")
        d["n"] += 1
        d["classified"] += got is not None
        d["agree"] += got == want
        if want == "action":
            d["acted"] += 1
            d["fg_hit"] += got == "action"
            d["floor_hit"] += floor[r["email_id"]].bucket == "action"
            if r["email_id"] in apple:
                d["apple_seen"] += 1
                d["apple_hit"] += apple[r["email_id"]]
        if got == "action":
            d["fg_action"] += 1
            d["fg_action_ok"] += want == "action"
        if r["email_id"] in apple and apple[r["email_id"]]:
            d["apple_marked"] += 1
            d["apple_marked_ok"] += want == "action"
        want_d = r["deadline"]
        got_d = (stored.get(r["email_id"]) or {}).get("deadline")
        if want_d:
            d["dated"] += 1
            d["date_ok"] += (got_d or "")[:10] == want_d
        else:
            d["undated"] += 1
            d["undated_ok"] += not got_d

    measured = bool(apple)
    languages = {}
    for lang, d in sorted(per.items()):
        languages[lang] = {
            "n": d["n"],
            "acted": d["acted"],
            "fg_act_recall": _ratio(d["fg_hit"], d["acted"]),
            "fg_act_precision": _ratio(d["fg_action_ok"], d["fg_action"]),
            "floor_act_recall": _ratio(d["floor_hit"], d["acted"]),
            "apple_act_recall": _ratio(d["apple_hit"], d["apple_seen"]) if measured else None,
            "apple_precision": _ratio(d["apple_marked_ok"], d["apple_marked"]) if measured else None,
            "bucket_agreement": _ratio(d["agree"], d["n"]),
            "date_recall": _ratio(d["date_ok"], d["dated"]),
            "no_date_ok": _ratio(d["undated_ok"], d["undated"]),
            "unclassified": d["n"] - d["classified"],
        }
    models = Counter((stored.get(r["email_id"]) or {}).get("model") or "" for r in rows)
    return {
        **progress(),
        "languages": languages,
        "apple": {"measured": measured, "reason": "" if measured else apple_reason,
                  "matched": len(apple)},
        "model": (models.most_common(1)[0][0] if models else ""),
    }


# ------------------------------------------------------------------- export

_CARD_KEYS = ("n", "acted", "fg_act_recall", "fg_act_precision", "floor_act_recall",
              "apple_act_recall", "apple_precision", "bucket_agreement", "date_recall",
              "no_date_ok", "unclassified")


def install_id() -> str:
    value = db.get_setting("quality_install_id", "")
    if not value:
        value = uuid.uuid4().hex
        db.set_setting("quality_install_id", value)
    return value


def scorecard() -> dict[str, Any]:
    """The file a tester sends back. Built from a whitelist of numbers, so no
    email text, address, id, path or date can reach it."""
    from .config import VERSION
    from .llm import registry
    s = score()
    return {
        "format": "foolsgold-quality-1",
        "app_version": VERSION,
        "provider": registry.configured_provider(),
        "model": str(s["model"])[:60] if re.match(r"^[\w.:/-]*$", str(s["model"])) else "",
        "install_id": install_id(),
        "labelled": int(s["labelled"]),
        "languages": {lang: {k: d[k] for k in _CARD_KEYS}
                      for lang, d in s["languages"].items() if lang in ("ko", "en", "mixed")},
        "apple": {"measured": bool(s["apple"]["measured"]),
                  "matched": int(s["apple"]["matched"]),
                  "reason": s["apple"]["reason"] if s["apple"]["reason"] in _REASONS else ""},
    }


_REASONS = {"", "no_index", "no_access", "no_apple_mail", "unreadable",
            "no_priority_column", "no_match"}
