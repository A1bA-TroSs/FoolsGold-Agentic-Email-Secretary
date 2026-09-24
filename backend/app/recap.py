"""The pile, before you read it.

The briefing answers *"what should I do next"* -- seven ranked rows, refilled as
they are settled. This answers a different question: *"I have not opened my
mail; what is in there?"* A report on the app's own decisions is not that
either. The subject of this card is the mail.

Three rules the rest of this module exists to keep.

**1. Every line carries the ids it was built from.** A line IS an email, or one
deduplicated announcement, so the link is structural: a wrong one is impossible
rather than unlikely. Nothing renders that cannot be opened. This is why there
is no prose paragraph here -- a sentence naming three things has to be anchored
span by span, and an anchor nobody checked is how these features invent
references.

**2. Nothing is generated at read time.** `category`, `deadline`, the extracted
tasks and the per-email `summary` are all written once, by the classify call
that already runs at sync. Assembling this card is SQL and grouping: no model
call, no 8-second wait, and it still works when `llm_provider` is `none`.

**3. Reading the card is not reading the mail.** Building a recap touches no
`is_read` flag, and it does not move the window. The window moves when the user
*dismisses* the card, and nowhere else -- see `mark_seen`. Conflating the two
would make the feature eat its own input: open the app to check one thing and
the next morning's summary is empty.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db, dedup, relevance

SEEN_KEY = "recap_last_seen_at"

# Below this, do not render a new card. Right after a dismissal the window is
# zero, and a card that keeps reappearing empty teaches the user to ignore it --
# which is the one failure a summary cannot come back from.
WINDOW_FLOOR_HOURS = 2

# Above this, stop pretending to enumerate an absence. The card says "since
# <date>" and shows the head of each section instead.
WINDOW_CEILING_DAYS = 7

# What "since you last looked" means before there is a watermark to look at.
FIRST_RUN_WINDOW_HOURS = 24

# How many lines a section may carry in the payload. The *collapse* -- how many
# are visible before "show 9 more" -- belongs to the frontend, because expanding
# a section must not cost a request: the card's job is orientation and a
# round-trip in the middle of it is a navigation away by another name.
SECTION_MAX = 50
BULK_SENDER_MAX = 12

# A verdict that means the user already dealt with this. It is not part of the
# pile any more, whatever the window says.
_SETTLED = ("done", "not_relevant", "snoozed")


# --------------------------------------------------------------------------
# the window
# --------------------------------------------------------------------------

def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat((value or "").strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def window(now: datetime | None = None) -> dict[str, Any]:
    """Since when, and is the card worth rendering at all.

    Absence-anchored, not clock-anchored: twenty minutes away is a small card,
    four days away is four days of mail. A calendar day is the wrong unit for
    "what did I miss" and it is the unit every mail product reaches for first.
    """
    now = now or datetime.now(timezone.utc)
    stored = _parse(db.get_setting(SEEN_KEY, ""))
    since = stored or (now - timedelta(hours=FIRST_RUN_WINDOW_HOURS))
    clamped = False
    ceiling = now - timedelta(days=WINDOW_CEILING_DAYS)
    if since < ceiling:
        since, clamped = ceiling, True
    return {
        "since": since.isoformat(),
        "clamped": clamped,
        "first_run": stored is None,
        # A floor, not a cache: the card is suppressed because there is nothing
        # to say, not because something was computed recently.
        "too_soon": stored is not None and (now - stored) < timedelta(hours=WINDOW_FLOOR_HOURS),
    }


def mark_seen(now: datetime | None = None) -> str:
    """The only thing in the app that moves the window.

    Called when the card is dismissed. Not when it is rendered, not when an
    email is opened from it, and not when the app starts."""
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    db.set_setting(SEEN_KEY, stamp)
    return stamp


# --------------------------------------------------------------------------
# display categories
# --------------------------------------------------------------------------

# `classifications.category` is the model's answer and it feeds the learned
# per-category weights. These patterns are NOT that, and are deliberately kept
# out of the column: they group rows on screen when the model has not labelled
# them -- an unclassified mailbox, or `llm_provider = none` -- and nothing
# learns from them. A display guess that leaked into the learning signal would
# attach the user's corrections to a drawer a keyword invented.
#
# Ordered most specific first. "announcement" is last because 공지/notice
# matches almost anything, and a broad pattern that runs early eats the
# narrower ones.
_DISPLAY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("exam", re.compile(
        r"\b(exam|midterm|final\s*exam|quiz|grade[sd]?|transcript)\b|시험|중간고사|기말|성적", re.I)),
    ("competition", re.compile(
        r"\b(hackathon|contest|competition|challenge|call\s+for\s+(?:application|entries))\b"
        r"|해커톤|공모전|대회", re.I)),
    ("career", re.compile(
        r"\b(career|internship|intern|recruit\w*|hiring|job\s*fair|co-?op|placement)\b"
        r"|채용|인턴|취업|커리어", re.I)),
    ("coursework", re.compile(
        r"\b(assignment|homework|submission|deliverable|lab\s*report|project\s*report)\b"
        r"|과제|제출|레포트", re.I)),
    ("event", re.compile(
        r"\b(seminar|workshop|colloquium|talk|webinar|symposium|ceremony)\b"
        r"|세미나|특강|행사|설명회", re.I)),
    ("admin", re.compile(
        r"\b(enrol\w*|registration|tuition|fee[s]?|visa|housing|dormitory|scholarship)\b"
        r"|수강신청|등록금|장학|기숙사|학사", re.I)),
    ("service", re.compile(
        r"\b(password|account|maintenance|downtime|ticket|vpn|wi-?fi|system\s+notice)\b"
        r"|비밀번호|계정|점검|장애", re.I)),
    ("announcement", re.compile(r"\b(notice|announcement|timetable|bulletin)\b|공지|안내문", re.I)),
)


def display_category(row: dict[str, Any]) -> str:
    """The section a row is shown under. Never written to the database."""
    stored = (row.get("category") or "").strip().lower()
    if stored in relevance.CATEGORIES:
        return stored
    subject = row.get("subject") or ""
    for name, pattern in _DISPLAY_PATTERNS:
        if pattern.search(subject):
            return name
    return ""


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

_COLUMNS = (
    "e.id, e.subject, e.from_name, e.from_address, e.received_at, e.is_read, "
    "e.conversation_id, c.bucket, c.deadline, c.category, c.summary, c.explored, "
    "f.verdict"
)


def _rows(since: str) -> list[dict[str, Any]]:
    """What arrived in the window and is still waiting.

    Read mail is out: the question is what piled up *unseen*, and something
    already opened on a phone has been seen. Settled mail is out for the same
    reason from the other direction. Muted senders are out because muting is a
    standing decision and a summary that re-surfaces them breaks the promise --
    they are counted in the suppressed footer instead.
    """
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM emails e "
            "LEFT JOIN classifications c ON c.email_id = e.id "
            "LEFT JOIN feedback f ON f.email_id = e.id "
            "WHERE datetime(e.received_at) > datetime(?) "
            "  AND e.is_read = 0 "
            "  AND (f.verdict IS NULL OR f.verdict NOT IN (?, ?, ?)) "
            "  AND e.from_address NOT IN (SELECT address FROM muted_senders) "
            "ORDER BY datetime(e.received_at) DESC",
            (since, *_SETTLED),
        ).fetchall()
    return [dict(r) for r in rows]


def _tasks_for(ids: list[str]) -> dict[str, str]:
    """The live to-do an email produced, if it produced one.

    This is the best summary line the app has for action mail and it is already
    written in the imperative -- "Submit the Co-op progress report" -- because
    the calendar needed it in that voice first."""
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT email_id, title, due_date FROM tasks "
            f"WHERE email_id IN ({marks}) AND deleted_at IS NULL AND status = 'open' "
            "ORDER BY due_date",
            ids,
        ).fetchall()
    out: dict[str, str] = {}
    for row in rows:
        out.setdefault(row["email_id"], row["title"])
    return out


def _line(row: dict[str, Any], members: list[dict[str, Any]], task: str) -> dict[str, Any]:
    """One line of the card.

    `email_ids` is the whole deduplicated group and `email_id` is the copy worth
    opening, so a three-times-sent announcement is one line that still knows
    about all three."""
    return {
        "email_id": row["id"],
        "email_ids": [m["id"] for m in members],
        "subject": row.get("subject") or "",
        # What the mail says, in this order of preference: the obligation it
        # created, then the model's one-line summary, then nothing. Never the
        # ranking rationale -- that explains the app's sorting, which is the
        # thing this card is explicitly not about.
        "task": task,
        "summary": (row.get("summary") or "").strip(),
        "sender": row.get("from_name") or row.get("from_address") or "",
        "deadline": row.get("deadline"),
        "received_at": row.get("received_at"),
        "bucket": row.get("bucket") or "fyi",
        "copies": len(members),
    }


def _section(key: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "key": key,
        "count": len(lines),
        "lines": lines[:SECTION_MAX],
        "truncated": len(lines) > SECTION_MAX,
    }


def build(now: datetime | None = None) -> dict[str, Any]:
    """The card. No model call, no writes."""
    now = now or datetime.now(timezone.utc)
    win = window(now)
    rows = _rows(win["since"])

    # One announcement is one line however many times the department sent it.
    # `group_ids` runs once over the whole window -- it is a union-find over the
    # batch, so calling it per row would be both wrong and quadratic.
    mapping = dedup.group_ids(rows)
    groups: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(mapping[row["id"]], []).append(row)

    heads = [members[0] for members in groups.values()]
    heads.sort(key=lambda r: r.get("received_at") or "", reverse=True)
    tasks = _tasks_for([r["id"] for r in heads])

    needs: list[dict[str, Any]] = []
    dated: list[dict[str, Any]] = []
    by_category: dict[str, list[dict[str, Any]]] = {}
    bulk: dict[str, list[dict[str, Any]]] = {}

    for head in heads:
        members = groups[mapping[head["id"]]]
        line = _line(head, members, tasks.get(head["id"], ""))
        bucket = head.get("bucket") or "fyi"
        if bucket == "action":
            needs.append(line)
        elif bucket == "noise":
            bulk.setdefault(line["sender"] or "?", []).append(line)
        elif head.get("deadline"):
            dated.append(line)
        else:
            by_category.setdefault(display_category(head), []).append(line)

    needs.sort(key=lambda l: (l["deadline"] or "9999-12-31", l["subject"]))
    dated.sort(key=lambda l: (l["deadline"] or "9999-12-31", l["subject"]))

    sections = [s for s in (_section("needs", needs), _section("dated", dated)) if s["count"]]

    # Sections ordered by what the user actually does with each kind, not by a
    # list someone typed. `category_evidence` already computes it, and the
    # ordering is inspectable for the same reason it was built: "you marked six
    # of eight hackathon invitations irrelevant" is a sentence you can disagree
    # with. A category the user has never seen sorts last rather than first.
    weight = {row["category"]: row.get("total", 0) for row in db.category_evidence()}
    for name in sorted(by_category, key=lambda n: (-weight.get(n, 0), n or "zz")):
        sections.append(_section(f"cat:{name}" if name else "cat:other", by_category[name]))

    bulk_section = None
    if bulk:
        senders = sorted(bulk.items(), key=lambda kv: -len(kv[1]))
        bulk_section = {
            "key": "bulk",
            "count": sum(len(v) for v in bulk.values()),
            "senders": [
                {"sender": sender, "count": len(lines), "lines": lines[:SECTION_MAX]}
                for sender, lines in senders[:BULK_SENDER_MAX]
            ],
            "more": max(0, len(senders) - BULK_SENDER_MAX),
        }

    return {
        "since": win["since"],
        "clamped": win["clamped"],
        "first_run": win["first_run"],
        "too_soon": win["too_soon"],
        "total": sum(len(v) for v in groups.values()),
        "lines": len(heads),
        "sections": sections,
        "bulk": bulk_section,
        "hidden": _hidden(win["since"]),
    }


def _hidden(since: str) -> dict[str, Any]:
    """One line, at the bottom, and deliberately not the point of the card.

    What the ranker suppressed and which guesses it surfaced anyway is real and
    the user is entitled to it -- but it is a report on the app, and this card
    is a report on the mail. It earns a footer, not a section."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT "
            "  SUM(CASE WHEN e.from_address IN (SELECT address FROM muted_senders) "
            "           OR c.bucket = 'noise' THEN 1 ELSE 0 END) AS hidden, "
            "  SUM(COALESCE(c.explored, 0)) AS explored "
            "FROM emails e LEFT JOIN classifications c ON c.email_id = e.id "
            "WHERE datetime(e.received_at) > datetime(?) AND e.is_read = 0",
            (since,),
        ).fetchone()
    return {"hidden": int(row["hidden"] or 0), "explored": int(row["explored"] or 0)}
