"""The calendar's data model.

One merged stream of things that are due on a given day, from two origins:

  * **email** -- a deadline the classifier read out of a message
    ("respond by 28 August"). These appear without the user doing anything.
  * **task**  -- a checklist item the user typed in.

They behave identically once they are on the calendar: tick to complete, and a
completed item is struck through *in place* rather than removed, because the
calendar is a record of the day as well as a plan for it. Only an explicit
delete takes something off.

Three things remove an email-derived entry, and all three are the user saying
the same thing in different words:

  * muting the sender      -- nothing from this address is worth tracking
  * deleting the entry     -- this particular date is not a real deadline
  * (ticking it done does *not* remove it; that is the whole point)

The date arithmetic here is deliberately done with `datetime.date` rather than
by string slicing. Month lengths, leap years and the Monday-vs-Sunday week
start are exactly the sort of thing that looks right in August and breaks in
February.
"""
from __future__ import annotations

import calendar as _stdlib_calendar
from datetime import date, timedelta
from typing import Any, Iterable

from . import db, priority

# The grid is always six rows. A month can span four, five or six weeks, and a
# grid that changes height as you page through the year makes everything below
# it jump. Six rows covers the worst case, so the layout never moves.
GRID_ROWS = 6
DAYS_IN_WEEK = 7


def parse_day(value: str | None) -> date | None:
    """Tolerant ISO date parse. The deadline column is filled by a language
    model on some paths, so it can contain anything at all; a bad value should
    drop one entry, not break the month."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """First and last day of the month itself (not of the grid)."""
    if not 1 <= month <= 12:
        raise ValueError("month must be 1-12")
    last = _stdlib_calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def grid_bounds(year: int, month: int, week_starts_on: int = 0) -> tuple[date, date]:
    """The full six-week span the month is drawn on, including the leading and
    trailing days that belong to the neighbouring months.

    `week_starts_on` is a `date.weekday()` value: 0 = Monday, 6 = Sunday."""
    first, _ = month_bounds(year, month)
    lead = (first.weekday() - week_starts_on) % DAYS_IN_WEEK
    start = first - timedelta(days=lead)
    return start, start + timedelta(days=GRID_ROWS * DAYS_IN_WEEK - 1)


def month_grid(year: int, month: int, week_starts_on: int = 0) -> list[list[date]]:
    start, _ = grid_bounds(year, month, week_starts_on)
    return [
        [start + timedelta(days=row * DAYS_IN_WEEK + col) for col in range(DAYS_IN_WEEK)]
        for row in range(GRID_ROWS)
    ]


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = (year * 12 + (month - 1)) + delta
    return index // 12, index % 12 + 1


# --------------------------------------------------------------------------
# entries
# --------------------------------------------------------------------------

def _email_entries(start: str, end: str) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT e.id, e.subject, e.from_name, e.from_address, e.received_at, "
            "       c.deadline, c.bucket, f.verdict, f.created_at AS acted_at "
            "FROM emails e JOIN classifications c ON c.email_id = e.id "
            "LEFT JOIN feedback f ON f.email_id = e.id "
            "WHERE c.deadline IS NOT NULL AND c.deadline != '' "
            "  AND e.from_address NOT IN (SELECT address FROM muted_senders) "
            "  AND e.id NOT IN (SELECT email_id FROM calendar_hidden) "
            "ORDER BY c.deadline, e.received_at DESC"
        ).fetchall()

    # An email whose body has been read into to-dos does not also need a chip
    # of its own. "Co-op Project Final Presentation Reminder" on the 30th tells
    # you nothing; "Submit the Co-op progress report" on the 30th is the thing
    # you actually have to do, and it is already on that day.
    superseded = db.email_task_ids()

    highlights = db.sender_highlights()

    out = []
    for row in rows:
        if row["id"] in superseded:
            continue
        due = parse_day(row["deadline"])
        if due is None or not (start <= due.isoformat() <= end):
            continue
        done = row["verdict"] == "done"
        out.append({
            "kind": "email",
            "key": f"email:{row['id']}",
            "id": row["id"],
            "email_id": row["id"],
            "title": row["subject"] or "(no subject)",
            "sender": row["from_name"] or row["from_address"] or "",
            # The address, not just the display name: muting is addressed at a
            # correspondent, and the calendar has to be able to name one.
            "sender_address": row["from_address"] or "",
            "highlight": highlights.get((row["from_address"] or "").lower()),
            "origin": "email",
            "bucket": row["bucket"],
            "due": due.isoformat(),
            "done": done,
            "completed_at": row["acted_at"] if done else None,
            "note": None,
        })
    return out


def _as_task_entry(row: dict[str, Any], highlights: dict[str, str] | None = None) -> dict[str, Any]:
    """A task row, whether the user typed it or we read it out of a message.

    They are the same kind of thing on the calendar -- tick it, move it, remove
    it -- and differ only in where they came from, which `origin` records and
    the UI shows as a label. One thing does differ: a task from a message keeps
    a way back to that message, and inherits its sender, so muting the sender
    takes its to-dos with it."""
    from_email = bool(row.get("email_id"))
    return {
        "kind": "task",
        "key": f"task:{row['id']}",
        "id": row["id"],
        "email_id": row.get("email_id"),
        "origin": row.get("origin") or "user",
        "title": row["title"],
        # Hand-written tasks have no sender, and so nothing to mute.
        "sender": (row.get("sender_name") or row.get("sender_address") or "") if from_email else None,
        "sender_address": row.get("sender_address") if from_email else None,
        # A to-do read out of a highlighted sender's mail wears that colour too,
        # or the calendar would colour the message and not the work it created.
        "highlight": (highlights or {}).get((row.get("sender_address") or "").lower())
                     if from_email else None,
        "bucket": None,
        "due": row["due_date"],
        "done": row["status"] == "done",
        "completed_at": row["completed_at"],
        "note": row["note"],
    }


def _task_entries(start: str, end: str) -> list[dict[str, Any]]:
    highlights = db.sender_highlights()
    return [_as_task_entry(row, highlights) for row in db.tasks_between(start, end)]


def _sort_key(entry: dict[str, Any]) -> tuple:
    # Within a day: still-open before completed, then email deadlines before
    # hand-written tasks, then alphabetical so the order never wobbles.
    return (entry["due"], entry["done"], entry["kind"] != "email", entry["title"].lower())


def entries(start: date, end: date) -> list[dict[str, Any]]:
    lo, hi = start.isoformat(), end.isoformat()
    merged = _email_entries(lo, hi) + _task_entries(lo, hi)
    return sorted(merged, key=_sort_key)


def group_by_day(items: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    days: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        days.setdefault(item["due"], []).append(item)
    return days


def month(year: int, month_number: int, week_starts_on: int = 0) -> dict[str, Any]:
    start, end = grid_bounds(year, month_number, week_starts_on)
    first, last = month_bounds(year, month_number)
    items = entries(start, end)
    return {
        "year": year,
        "month": month_number,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "month_start": first.isoformat(),
        "month_end": last.isoformat(),
        "week_starts_on": week_starts_on,
        "weeks": [[d.isoformat() for d in week] for week in month_grid(year, month_number, week_starts_on)],
        "days": group_by_day(items),
        "today": date.today().isoformat(),
    }


def agenda(day: date | None = None) -> dict[str, Any]:
    """What a reminder should say. Today's items, plus anything already overdue
    and still open -- a checklist that silently drops what you missed yesterday
    is worse than no checklist."""
    day = day or date.today()
    today = day.isoformat()
    todays = [e for e in entries(day, day)]
    # Carrying missed work forward is the point; carrying it forever is nagging.
    # The window is the same month-long horizon the briefing uses, so a
    # reminder never mentions something the app has otherwise stopped raising.
    horizon = day - timedelta(days=priority.FORGET_AFTER_DAYS)
    overdue = [e for e in entries(horizon, day - timedelta(days=1)) if not e["done"]]
    return {
        "date": today,
        "weekday": day.weekday(),           # 0 = Monday
        "open": [e for e in todays if not e["done"]],
        "done": [e for e in todays if e["done"]],
        "overdue": overdue,
    }


# --------------------------------------------------------------------------
# mutations
# --------------------------------------------------------------------------

def set_done(kind: str, ident: str, done: bool) -> dict[str, Any]:
    """Tick or untick, whichever origin the entry has.

    Unticking an email clears the feedback row only when it actually says
    'done'. Blanket-clearing would also throw away a pin the user set earlier,
    which they never asked us to touch."""
    if kind == "task":
        row = db.update_task(int(ident), status="done" if done else "open")
        if row is None:
            raise KeyError(ident)
        return {"kind": "task", "id": row["id"], "done": row["status"] == "done"}

    if kind == "email":
        if done:
            db.set_feedback(ident, "done")
        else:
            current = db.all_feedback().get(ident)
            if current and current.get("verdict") == "done":
                db.clear_feedback(ident)
        return {"kind": "email", "id": ident, "done": done}

    raise ValueError(f"unknown entry kind: {kind}")


def remove(kind: str, ident: str) -> dict[str, Any]:
    """Take something off the calendar. Reversible either way -- both origins
    land in the removed box rather than disappearing, because "this is not a
    deadline" is a judgement people revise."""
    if kind == "task":
        if not db.delete_task(int(ident)):
            raise KeyError(ident)
        return {"kind": "task", "id": ident, "removed": True}
    if kind == "email":
        db.hide_calendar_email(ident)
        return {"kind": "email", "id": ident, "removed": True}
    raise ValueError(f"unknown entry kind: {kind}")


def restore(kind: str, ident: str) -> dict[str, Any]:
    """Back to the day it was filed on. Nothing about a due date changed while
    it sat in the removed box, so there is no date to recompute."""
    if kind == "task":
        if not db.restore_task(int(ident)):
            raise KeyError(ident)
        return {"kind": "task", "id": ident, "removed": False}
    if kind == "email":
        if not db.unhide_calendar_email(ident):
            raise KeyError(ident)
        return {"kind": "email", "id": ident, "removed": False}
    raise ValueError(f"unknown entry kind: {kind}")


def purge(kind: str, ident: str) -> dict[str, Any]:
    """The only irreversible action in the calendar, and it is two clicks deep
    inside the removed box.

    For a task this destroys the row. For an email it destroys only the
    *tombstone* -- which puts the due date back on the calendar, and would be
    the opposite of what "delete forever" implies. So an email entry cannot be
    purged: there is nothing of ours to destroy. Mute the sender instead."""
    if kind == "task":
        if not db.purge_task(int(ident)):
            raise KeyError(ident)
        return {"kind": "task", "id": ident, "purged": True}
    if kind == "email":
        raise ValueError("an email's due date cannot be purged; mute the sender instead")
    raise ValueError(f"unknown entry kind: {kind}")


def removed() -> list[dict[str, Any]]:
    """Everything currently off the calendar by the user's hand, newest first.

    Muted senders are *not* listed here. Muting is undone in the muted box, by
    address, and listing each of its dates separately would offer a restore
    that silently does nothing -- the mute filter would hide the entry again."""
    out: list[dict[str, Any]] = []

    for row in db.hidden_calendar_rows():
        due = parse_day(row["deadline"])
        out.append({
            "kind": "email",
            "key": f"email:{row['email_id']}",
            "id": row["email_id"],
            "email_id": row["email_id"],
            "title": row["subject"] or "(no subject)",
            "sender": row["from_name"] or row["from_address"] or "",
            "sender_address": row["from_address"] or "",
            "due": due.isoformat() if due else None,
            "removed_at": row["removed_at"],
            "can_purge": False,
        })

    highlights = db.sender_highlights()
    for row in db.removed_tasks():
        entry = _as_task_entry(row, highlights)
        entry["removed_at"] = row["deleted_at"]
        entry["can_purge"] = True
        out.append(entry)

    return sorted(out, key=lambda e: e["removed_at"] or "", reverse=True)
