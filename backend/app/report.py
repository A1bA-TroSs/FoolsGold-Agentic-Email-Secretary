"""The morning report (roadmap F6): one card instead of two.

The app had two summaries answering overlapping questions: the briefing
(`pipeline.build_digest`, "what to do next") and the recap (`recap.py`, "what
is in the pile since you last looked"). Neither led with the pain the user
research named: replies put off and deadlines lost. This card leads with
those, then what was missed, then the rest of the pile.

  1. replies   -- exactly `to_respond.owed()`, so this card and the To respond
                  list can never disagree;
  2. deadlines -- open mail due from DEADLINE_BACK_DAYS ago to
                  DEADLINE_AHEAD_DAYS ahead, soonest first;
  3. missed    -- important mail (action, or a must-see kind) still unopened
                  after a day, up to a week old;
  4. pile      -- `recap.build`, minus every email already shown above.

No email appears twice. Every line carries the id of the email it came from.
No model call: the report is SQL, the To respond rule and the recap grouping,
so it is the same with AI off, on, or failing (Danny, 2026-11-03, steer:
"one daily overall summary of missed mail and what needs checking"). It is
never emailed or sent anywhere.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import db, recap, relevance, to_respond

DEADLINE_BACK_DAYS = 2      # same grace as relevance.DATED_ASK_GRACE_DAYS
DEADLINE_AHEAD_DAYS = 7
# A deadline read out of mail older than this is not "due soon", whatever date
# came out of it (Danny's 2022 seminar notice read as due this week).
DEADLINE_MAIL_MAX_AGE_DAYS = 60
MISSED_AFTER_HOURS = 24
MISSED_WITHIN_DAYS = 7
_CLOSED = ("done", "not_relevant", "not_important")

OPENED_KEY = "report_opened_days"
ACTIVE_KEY = "report_active_days"
_DAYS_KEPT = 120


def _open_rows(where: str, params: tuple) -> list[dict[str, Any]]:
    """Mail still waiting on the user: not noise, not muted, not closed, not
    snoozed into the future. The rest of the condition is the caller's."""
    with db.connect() as conn:
        rows = conn.execute(
            # The body and recipients too: must_see() reads the opt-out footer
            # and the booking-reference proof from them, as it does at sync.
            "SELECT e.id, e.subject, e.from_name, e.from_address, e.received_at, e.is_read, "
            "       e.to_recipients, e.cc_recipients, e.body_text, e.body_preview, "
            "       c.bucket, c.deadline, f.verdict, f.snooze_until "
            "FROM emails e JOIN classifications c ON c.email_id = e.id "
            "LEFT JOIN feedback f ON f.email_id = e.id "
            "WHERE c.bucket != 'noise' "
            "  AND e.from_address NOT IN (SELECT address FROM muted_senders) "
            "  AND (f.verdict IS NULL OR f.verdict NOT IN (?, ?, ?)) "
            f"  AND {where}",
            (*_CLOSED, *params),
        ).fetchall()
    return [dict(r) for r in rows]


def _snoozed(row: dict[str, Any], now: datetime) -> bool:
    if row.get("verdict") != "snoozed":
        return False
    until = to_respond._when(row.get("snooze_until"))
    return until is None or until > now


def _line(row: dict[str, Any], **extra: Any) -> dict[str, Any]:
    line = {"email_id": row["id"], "subject": row.get("subject") or "",
            "sender": row.get("from_name") or row.get("from_address") or "",
            "received_at": row.get("received_at"), "deadline": row.get("deadline"),
            "bucket": row.get("bucket")}
    return {**line, **extra}


def when(deadline: str, today: date) -> str:
    day = date.fromisoformat(deadline[:10])
    if day < today:
        return "overdue"
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    return "this_week"


def deadlines(today: date, now: datetime, shown: set[str]) -> list[dict[str, Any]]:
    lo = (today - timedelta(days=DEADLINE_BACK_DAYS)).isoformat()
    hi = (today + timedelta(days=DEADLINE_AHEAD_DAYS)).isoformat()
    oldest = (now - timedelta(days=DEADLINE_MAIL_MAX_AGE_DAYS)).isoformat()
    rows = _open_rows("c.deadline IS NOT NULL AND substr(c.deadline, 1, 10) BETWEEN ? AND ? "
                      "AND datetime(e.received_at) >= datetime(?)", (lo, hi, oldest))
    rows = [r for r in rows if r["id"] not in shown and not _snoozed(r, now)]
    rows.sort(key=lambda r: (r["deadline"][:10], -(datetime.fromisoformat(
        (r.get("received_at") or "1970-01-01").replace("Z", "+00:00")).timestamp())))
    return [_line(r, when=when(r["deadline"], today)) for r in rows]


def missed(now: datetime, shown: set[str]) -> list[dict[str, Any]]:
    """Important mail the user has not opened for more than a day."""
    old = (now - timedelta(days=MISSED_WITHIN_DAYS)).isoformat()
    new = (now - timedelta(hours=MISSED_AFTER_HOURS)).isoformat()
    rows = _open_rows("e.is_read = 0 AND datetime(e.received_at) BETWEEN datetime(?) AND datetime(?)",
                      (old, new))
    out = []
    for r in sorted(rows, key=lambda r: r.get("received_at") or "", reverse=True):
        if r["id"] in shown or _snoozed(r, now):
            continue
        kind = relevance.must_see(r)
        if r.get("bucket") == "action" or kind:
            out.append(_line(r, kind=kind))
    return out


def _pile(now: datetime, shown: set[str]) -> dict[str, Any]:
    """The recap, minus what the sections above already show. A line whose
    deduplicated group touches a shown email is dropped whole: the copy above
    stands for it."""
    # Built in full for the rail view, which the user asked for; `too_soon`
    # keeps the real window's answer so the card on top still stays quiet for
    # two hours after "mark as seen", as the recap card did.
    card = recap.build(now=now, force=True)
    card["too_soon"] = recap.window(now)["too_soon"]

    def keep(line: dict[str, Any]) -> bool:
        return not (set(line.get("email_ids") or [line["email_id"]]) & shown)

    sections = []
    for s in card["sections"]:
        lines = [l for l in s["lines"] if keep(l)]
        if lines:
            sections.append({**s, "lines": lines, "count": len(lines)})
    bulk = card.get("bulk")
    if bulk:
        senders = []
        for s in bulk["senders"]:
            lines = [l for l in s["lines"] if keep(l)]
            if lines:
                senders.append({**s, "lines": lines, "count": len(lines)})
        bulk = {**bulk, "senders": senders, "count": sum(s["count"] for s in senders)} if senders else None
    lines = sum(s["count"] for s in sections) + (bulk["count"] if bulk else 0)
    # "{n} waiting" in the pile header counts what the pile lists, copies included.
    total = sum(l.get("copies", 1) for s in sections for l in s["lines"]) + sum(
        l.get("copies", 1) for s in (bulk or {}).get("senders", []) for l in s["lines"])
    return {**card, "sections": sections, "bulk": bulk, "lines": lines, "total": total}


def _reply_lines(replies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """owed() rows in the report's line shape: who is waiting, and the
    deadline when the mail has one. Ids, order and reasons are unchanged."""
    ids = [r["id"] for r in replies]
    due: dict[str, str] = {}
    if ids:
        with db.connect() as conn:
            marks = ",".join("?" * len(ids))
            due = {r[0]: r[1] for r in conn.execute(
                f"SELECT email_id, deadline FROM classifications WHERE email_id IN ({marks})", ids)}
    return [_line(r, reason=r["reason"], deadline=due.get(r["id"])) for r in replies]


def build(now: datetime | None = None, today: date | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    today = today or date.today()
    _note_day(ACTIVE_KEY, today)

    replies = to_respond.owed(now=now)
    shown = {r["id"] for r in replies}
    dl = deadlines(today, now, shown)
    shown |= {r["email_id"] for r in dl}
    ms = missed(now, shown)
    shown |= {r["email_id"] for r in ms}
    pile = _pile(now, shown)

    counts = {"replies": len(replies), "due": len(dl), "missed": len(ms), "new": pile["lines"]}
    return {
        "headline": {"key": "reportHeadline" if any(counts.values()) else "reportAllClear",
                     "vars": counts},
        "replies": {"items": _reply_lines(replies), "count": len(replies)},
        "deadlines": {"items": dl, "count": len(dl)},
        "missed": {"items": ms, "count": len(ms)},
        "pile": pile,
    }


# --------------------------------------------------------------------------
# open rate, kept on this machine
# --------------------------------------------------------------------------

def _days(key: str) -> list[str]:
    try:
        days = json.loads(db.get_setting(key, "[]") or "[]")
    except ValueError:
        return []
    return [d for d in days if isinstance(d, str)]


def _note_day(key: str, today: date) -> None:
    days = _days(key)
    stamp = today.isoformat()
    if stamp not in days:
        db.set_setting(key, json.dumps((days + [stamp])[-_DAYS_KEPT:]))


def mark_opened(today: date | None = None) -> dict[str, int]:
    """The user opened something from the report today. Once per day."""
    _note_day(OPENED_KEY, today or date.today())
    return open_rate()


def open_rate() -> dict[str, int]:
    """Days the report was opened, of days it was shown. Numbers only."""
    return {"opened_days": len(_days(OPENED_KEY)), "active_days": len(_days(ACTIVE_KEY))}
