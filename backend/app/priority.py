"""Priority scoring.

The premise from the design spec: importance in a real inbox is contextual and
time-varying, not a fixed ranking of senders. So the score is built from four
independent signals -- what the user says they are working on right now, how
actionable the mail is, how close its deadline is, and structural facts about
the message -- rather than from a static sender allowlist.

Everything here is deterministic and testable, and the structural half runs with
no LLM at all, which is what keeps the list sensibly sorted when AI is off,
rate-limited, or offline.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from . import db

BUCKET_WEIGHT = {"action": 45.0, "fyi": 15.0, "noise": 0.0}

# Signals about what the user actually DID. These beat anything inferred from
# the text, because they are the user's own behaviour rather than a guess.
ANSWERED_PENALTY = -30.0    # you replied; whatever it asked for, you did it
FLAGGED_BONUS = 22.0        # you starred it
AFFINITY_CAP = 12.0         # mail from people you actually write back to
STALE_READ_PENALTY = -12.0  # read weeks ago, never flagged, never answered

# Explicit verdicts from the UI. Ground truth, so they dominate.
FEEDBACK_POINTS = {"pinned": 60.0, "not_important": -45.0, "done": -60.0}
NOISE_CEILING = 20.0
PRIORITY_MATCH_CAP = 30.0
STATUS_MULTIPLIER = {"active": 1.0, "low_care": -0.6, "dismissed": -1.2}

_STOPWORDS = {
    "the", "and", "for", "you", "your", "our", "with", "from", "this", "that", "are",
    "was", "will", "has", "have", "not", "but", "all", "can", "out", "new", "one",
    "get", "now", "week", "day", "please", "hi", "hello", "dear", "thanks", "thank",
    "re", "fw", "fwd", "update", "info", "email", "mail", "message", "regarding",
    "team", "notification", "no", "reply", "noreply", "do", "not", "com", "org",
}

_NOISE_SENDER = re.compile(
    r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|newsletter|mailer|notifications?|"
    r"marketing|updates?|alerts?|billing|receipts?|support)@", re.I
)
_NOISE_SUBJECT = re.compile(
    r"\b(unsubscribe|newsletter|webinar|sale|% ?off|deal|promo|digest|"
    r"weekly round[- ]?up|invoice|receipt|order (?:confirm|ship))\b", re.I
)
_ACTION_SUBJECT = re.compile(
    r"\b(action required|urgent|asap|deadline|due|reminder|rsvp|respond|reply|"
    r"confirm|approve|approval|sign|submit|complete|required|request(?:ed)?|"
    r"overdue|final notice|last chance to)\b", re.I
)

# Deadline phrasings we can resolve without a model.
_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_DMY = re.compile(
    r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(20\d{2})?\b", re.I
)
_MDY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(20\d{2})?\b", re.I
)
_WEEKDAY = re.compile(
    r"\bby\s+(?:next\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I
)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_WEEKDAYS = {d: i for i, d in enumerate(
    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])}


# --------------------------------------------------------------------------
# deadline extraction (structural fallback; the LLM does better when available)
# --------------------------------------------------------------------------

# A month. Past that a deadline is history, not a reminder: either it was met,
# or it was missed and the world moved on. The scoring curve below already
# stops *rewarding* an overdue date after three weeks; this is the separate,
# blunter rule about when we stop *mentioning* it at all -- in the briefing, in
# the reminders, and on the row chips. "496 days overdue" is not a to-do.
FORGET_AFTER_DAYS = 30


def is_forgotten(deadline: str | None, today: date | None = None) -> bool:
    """True when a due date is too far past to be worth raising."""
    if not deadline:
        return False
    try:
        due = date.fromisoformat(str(deadline)[:10])
    except (ValueError, TypeError):
        return False
    return (today or date.today()) - due > timedelta(days=FORGET_AFTER_DAYS)


def extract_deadline(text: str, today: date | None = None) -> str | None:
    if not text:
        return None
    today = today or date.today()
    window = text[:4000]

    match = _ISO_DATE.search(window)
    if match:
        return _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))

    match = _DMY.search(window)
    if match:
        day, month = int(match.group(1)), _MONTHS[match.group(2).lower()[:3]]
        year = int(match.group(3)) if match.group(3) else _infer_year(month, day, today)
        return _safe_date(year, month, day)

    match = _MDY.search(window)
    if match:
        month, day = _MONTHS[match.group(1).lower()[:3]], int(match.group(2))
        year = int(match.group(3)) if match.group(3) else _infer_year(month, day, today)
        return _safe_date(year, month, day)

    match = _WEEKDAY.search(window)
    if match:
        target = _WEEKDAYS[match.group(1).lower()]
        ahead = (target - today.weekday()) % 7 or 7
        return (today + timedelta(days=ahead)).isoformat()

    return None


def _infer_year(month: int, day: int, today: date) -> int:
    """A bare 'March 3' in an email is almost always the next occurrence."""
    candidate = _safe_date(today.year, month, day)
    if candidate and date.fromisoformat(candidate) < today - timedelta(days=180):
        return today.year + 1
    return today.year


def _safe_date(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# structural classification (no LLM)
# --------------------------------------------------------------------------

def structural_bucket(email: dict[str, Any], user_address: str = "") -> str:
    subject = email.get("subject") or ""
    sender = email.get("from_address") or ""
    body = (email.get("body_text") or email.get("body_preview") or "")[:2000]

    if _NOISE_SENDER.search(sender) or _NOISE_SUBJECT.search(subject):
        return "noise"
    if _ACTION_SUBJECT.search(subject) or _ACTION_SUBJECT.search(body):
        return "action"
    if user_address and _addressed_directly(email, user_address) and "?" in subject:
        return "action"
    return "fyi"


def _recipient_addresses(value: Any) -> list[str]:
    """Addresses out of a stored recipient list.

    Sources hand us `[{"name": ..., "address": ...}]`, but this column is JSON
    written by whichever source produced the message, and a bare list of
    strings is the obvious other shape. Scoring runs inside classification, so
    an AttributeError here does not merely mislabel one email -- it aborts the
    batch. Take what is recognisable and ignore the rest."""
    out = []
    for rec in db.json_list(value):
        if isinstance(rec, dict):
            address = rec.get("address") or rec.get("emailAddress") or ""
        elif isinstance(rec, str):
            address = rec
        else:
            continue
        address = str(address).strip().lower()
        if address:
            out.append(address)
    return out


def _addressed_directly(email: dict[str, Any], user_address: str) -> bool:
    """To: me is a far stronger signal than Cc: me -- this is the single most
    reliable structural cue an assistant has."""
    if not user_address:
        return False
    return user_address.lower() in _recipient_addresses(email.get("to_recipients"))


def _cc_only(email: dict[str, Any], user_address: str) -> bool:
    if not user_address or _addressed_directly(email, user_address):
        return False
    return user_address.lower() in _recipient_addresses(email.get("cc_recipients"))


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def match_priorities(email: dict[str, Any], priorities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keyword match as a floor. The LLM's semantic match is better and is
    unioned in by score_email; this makes sure a topic still registers when
    AI is unavailable."""
    haystack = " ".join([
        email.get("subject") or "",
        email.get("from_name") or "",
        email.get("from_address") or "",
        (email.get("body_text") or email.get("body_preview") or "")[:3000],
    ]).lower()
    hits = []
    for prio in priorities:
        topic = (prio.get("topic") or "").strip().lower()
        if not topic:
            continue
        terms = [t for t in re.split(r"[^a-z0-9]+", topic) if len(t) > 2 and t not in _STOPWORDS]
        if not terms:
            continue
        if all(t in haystack for t in terms) or topic in haystack:
            hits.append(prio)
    return hits


def deadline_points(deadline: str | None, today: date | None = None) -> float:
    """Points for how close a deadline is -- and, crucially, how *stale* it is.

    An overdue deadline used to score a flat +26 forever, which is why a print
    notice whose collection window closed in May could outrank this morning's
    mail: +26 beat the maximum recency bonus of +8, permanently. A deadline you
    blew through weeks ago was either handled elsewhere or is dead; either way it
    is not the most important thing in the inbox today. So it decays.
    """
    if not deadline:
        return 0.0
    today = today or date.today()
    try:
        due = date.fromisoformat(deadline)
    except ValueError:
        return 0.0

    days = (due - today).days
    if days < 0:
        overdue = -days
        if overdue <= 2:
            return 24.0      # just missed -- still very likely actionable
        if overdue <= 7:
            return 13.0
        if overdue <= 21:
            return 5.0
        return 0.0           # a month past due is history, not a priority
    if days == 0:
        return 30.0
    if days <= 2:
        return 22.0
    if days <= 7:
        return 12.0
    if days <= 14:
        return 5.0
    return 0.0


def affinity_points(from_address: str, correspondents: dict[str, int] | None) -> float:
    """Mail from someone the user actually writes back to.

    Deliberately flattened: someone written to 40 times is not four times more
    important than someone written to 10 times, and without a cap a single
    chatty thread would dominate the whole inbox.
    """
    if not correspondents or not from_address:
        return 0.0
    count = correspondents.get(from_address.lower(), 0)
    if count <= 0:
        return 0.0
    if count == 1:
        return 5.0
    if count <= 4:
        return 8.0
    return AFFINITY_CAP


def score_email(
    email: dict[str, Any],
    bucket: str,
    deadline: str | None,
    priorities: list[dict[str, Any]],
    llm_matched: Iterable[str] = (),
    user_address: str = "",
    today: date | None = None,
    correspondents: dict[str, int] | None = None,
    verdict: str | None = None,
    sender_muted: bool = False,
) -> tuple[float, list[str]]:
    """Returns (score, matched topic names). Deterministic -- unit tested."""
    # A muted sender is a standing decision about a correspondent, so it short
    # circuits every other signal. Nothing they send should compete for a place
    # in the list -- that is the whole point of muting them.
    if sender_muted:
        return 0.0, []

    today = today or date.today()
    score = BUCKET_WEIGHT.get(bucket, 15.0)
    score += deadline_points(deadline, today)

    by_topic = {(p.get("topic") or "").lower(): p for p in priorities}
    matched_records: dict[str, dict[str, Any]] = {}
    for prio in match_priorities(email, priorities):
        matched_records[(prio.get("topic") or "").lower()] = prio
    for name in llm_matched or ():
        record = by_topic.get(str(name).strip().lower())
        if record:
            matched_records[(record.get("topic") or "").lower()] = record

    priority_points = 0.0
    for record in matched_records.values():
        weight = float(record.get("weight") or 10)
        priority_points += weight * STATUS_MULTIPLIER.get(record.get("status") or "active", 1.0)
    score += max(-25.0, min(priority_points, PRIORITY_MATCH_CAP))

    # What the user actually did with it. These are behaviour, not inference.
    if email.get("is_answered"):
        score += ANSWERED_PENALTY
    if email.get("is_flagged"):
        score += FLAGGED_BONUS
    score += affinity_points(email.get("from_address") or "", correspondents)
    score += _stale_read_points(email, today)

    # Structural signals
    if _addressed_directly(email, user_address):
        score += 10.0
    elif _cc_only(email, user_address):
        score += 2.0
    importance = (email.get("importance") or "normal").lower()
    if importance == "high":
        score += 8.0
    elif importance == "low":
        score -= 5.0
    if email.get("has_attachments"):
        score += 3.0
    if not email.get("is_read"):
        score += 4.0

    # Recency: a three-week-old FYI should not outrank this morning's mail.
    score += _recency_points(email.get("received_at"))

    if bucket == "noise":
        score = min(score, NOISE_CEILING)

    # An explicit verdict from the UI overrides every inferred signal, including
    # the noise ceiling -- if the user pinned something, they meant it.
    if verdict in FEEDBACK_POINTS:
        score += FEEDBACK_POINTS[verdict]

    return round(max(score, 0.0), 2), sorted(
        record.get("topic") for record in matched_records.values() if record.get("topic")
    )


def _stale_read_points(email: dict[str, Any], today: date | None = None) -> float:
    """Old, read, never starred, never answered -- almost certainly finished with.

    Without this, anything that once looked urgent stays near the top of the list
    for as long as it sits in the mailbox, which is exactly how a priority inbox
    turns back into an ordinary one.
    """
    if email.get("is_flagged") or email.get("is_answered"):
        return 0.0
    if not email.get("is_read"):
        return 0.0
    age = _age_days(email.get("received_at"))
    if age is None or age < 21:
        return 0.0
    return STALE_READ_PENALTY if age < 60 else STALE_READ_PENALTY * 1.5


def _age_days(received_at: str | None) -> float | None:
    if not received_at:
        return None
    try:
        received = datetime.fromisoformat(str(received_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - received).total_seconds() / 86400


def _recency_points(received_at: str | None) -> float:
    age_days = _age_days(received_at)
    if age_days is None:
        return 0.0
    if age_days <= 1:
        return 8.0
    if age_days <= 3:
        return 5.0
    if age_days <= 7:
        return 2.0
    return 0.0


# --------------------------------------------------------------------------
# candidate topic discovery
# --------------------------------------------------------------------------

def suggest_topics(days: int = 60, limit: int = 12) -> list[dict[str, Any]]:
    """Surface recurring senders and subject terms from recent mail that the
    user has not yet ruled on. Offered on demand in Settings -- never pushed at
    them, per the design spec."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT subject, from_name, from_address FROM emails WHERE received_at >= ?",
            (cutoff,),
        ).fetchall()
        known = {
            (r["topic"] or "").lower()
            for r in conn.execute("SELECT topic FROM priorities").fetchall()
        }

    sender_counts: dict[str, dict[str, Any]] = {}
    term_counts: dict[str, int] = {}
    for row in rows:
        address = (row["from_address"] or "").lower()
        if address and not _NOISE_SENDER.search(address):
            label = row["from_name"] or address
            entry = sender_counts.setdefault(address, {"label": label, "count": 0})
            entry["count"] += 1
        for term in re.split(r"[^a-zA-Z0-9]+", (row["subject"] or "").lower()):
            if len(term) > 3 and term not in _STOPWORDS and not term.isdigit():
                term_counts[term] = term_counts.get(term, 0) + 1

    out: list[dict[str, Any]] = []
    for address, entry in sorted(sender_counts.items(), key=lambda kv: -kv[1]["count"]):
        if entry["count"] < 3 or entry["label"].lower() in known or address in known:
            continue
        out.append({"topic": entry["label"], "kind": "sender", "count": entry["count"], "detail": address})
        if len(out) >= limit // 2:
            break

    for term, count in sorted(term_counts.items(), key=lambda kv: -kv[1]):
        if count < 3 or term in known:
            continue
        out.append({"topic": term, "kind": "subject", "count": count, "detail": f"{count} emails"})
        if len(out) >= limit:
            break
    return out


def active_priorities() -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM priorities WHERE status != 'dismissed' ORDER BY weight DESC, topic"
        ).fetchall()
    return [dict(r) for r in rows]


def priority_topics() -> list[str]:
    return [p["topic"] for p in active_priorities() if p.get("status") == "active"]
