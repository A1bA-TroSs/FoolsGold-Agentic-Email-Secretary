"""One announcement, one row.

Departments send a thing, then remind you about it, then send the reminder
again. Measured on the real mailbox: **1,143 ranked candidates collapse to
837** — 306 rows that say something the list already said.

Three shapes, and they need three different tests, which is why this is a
module rather than a key function:

| shape | example | caught by |
|---|---|---|
| a thread | `Re:` / `答复:` on one conversation | `conversation_id` |
| a re-send | `HKUST Daily Event Alert`, 113 times | sender + normalised subject |
| a reminder | `Reminder: X` beside `X` | containment, same sender and deadline |

They overlap — a thread can also be a subject repeat — so membership is a
union-find rather than a single key. Without it a thread whose members carry
different `conversation_id`s (some messages have none) splits into two groups
and the duplicate survives.

**This is not the calendar's rule and must not become it.** There the key is
`(day, subject)` and the day is deliberately part of it, because a weekly
seminar is a real recurrence and the calendar exists to put each occurrence on
its own square. A ranked list has no day to disambiguate with: two rows reading
`HKUST Daily Event Alert` tell the reader nothing the count does not. What the
two surfaces *do* share is the subject normaliser, which lives here so it
cannot drift — the only copy of a rule this repository has ever had drift
quietly was the deadline arithmetic, and that drift was the bug.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable

from .compose.message import _FORWARD_RE, _REPLY_RE, strip_prefixes

# Below this, a substring match is a coincidence rather than a containment.
# "Re: Hi" inside "Re: Hi there" is not evidence of anything.
MIN_CONTAINED = 12


def normalise_subject(subject: str) -> str:
    """The comparable form of a subject line.

    Reply and forward markers come off first, so `X`, `Re: X` and `答复: X` are
    one announcement — `strip_prefixes` already knows the markers in fourteen
    languages, which is exactly why this uses it instead of matching `Re:`.
    """
    text = strip_prefixes(subject or "", _REPLY_RE)
    text = strip_prefixes(text, _FORWARD_RE)
    return re.sub(r"\s+", " ", text).strip().casefold().rstrip(" .!?。")


class _Groups:
    """Union-find, small enough to own rather than depend on."""

    def __init__(self) -> None:
        self.parent: dict[Any, Any] = {}

    def find(self, item: Any) -> Any:
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:      # path compression
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def group_ids(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Map each row id to the id of the group it belongs to."""
    rows = list(rows)
    groups = _Groups()
    for row in rows:
        me = ("email", row["id"])
        groups.find(me)
        thread = (row.get("conversation_id") or "").strip()
        if thread:
            groups.union(me, ("thread", thread))
        groups.union(me, ("subject",
                          (row.get("from_address") or "").lower(),
                          normalise_subject(row.get("subject") or "")))

    # The reminder pass, and the narrowest of the three on purpose: same
    # sender, same deadline, and one subject line contains the other after
    # normalisation. On the mailbox this was written against it made 14 merges
    # and every one was correct -- `Reminder: X` with `X`, `[Reminder] X` with
    # `X`, `[IEI Event of Today] X` with `X`, `[活動提示] X` with `X`. A
    # deadline has to match because without it the containment test is just
    # "one subject is inside another", which two genuinely different notices
    # from one department can satisfy.
    by_sender_day: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = {}
    for row in rows:
        deadline = row.get("deadline")
        if not deadline:
            continue
        key = ((row.get("from_address") or "").lower(), deadline)
        by_sender_day.setdefault(key, []).append(
            (normalise_subject(row.get("subject") or ""), row))
    for bucket in by_sender_day.values():
        for i, (subject_a, row_a) in enumerate(bucket):
            for subject_b, row_b in bucket[i + 1:]:
                if subject_a == subject_b:
                    continue
                if len(subject_a) < MIN_CONTAINED or len(subject_b) < MIN_CONTAINED:
                    continue
                if subject_a in subject_b or subject_b in subject_a:
                    groups.union(("email", row_a["id"]), ("email", row_b["id"]))

    return {row["id"]: groups.find(("email", row["id"])) for row in rows}


def collapse(
    rows: Iterable[dict[str, Any]],
    *,
    rank: Callable[[dict[str, Any]], Any] | None = None,
    drop_groups: Callable[[dict[str, Any]], bool] | None = None,
    eligible: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """One row per announcement, in the order they arrived.

    **The best-ranked copy survives, not the newest**, and that is the
    difference from the calendar. Collapsing must not reorder: the ICAC seminar
    this was measured against is ranked second in the list and its newest copy
    scores sixteen points lower, so keeping the newest would have quietly
    demoted it out of the top twenty. Dedup removes rows; it does not decide
    what matters. Ties go to the most recent, because between two equal copies
    a reminder supersedes what it reminds you of.

    `drop_groups` takes a row and answers "is this whole announcement finished
    with". It exists because **ticking the survivor must not promote a
    sibling**: the copies the user never saw are still open, so without this
    the row they just cleared comes straight back wearing a different id.

    `eligible` separates *what can be shown* from *what counts as a copy*. A
    snoozed message is still a copy of the announcement and still says
    something about how often it has arrived, but it must not be the row the
    list shows -- the user put it away. Grouping sees everything; only eligible
    rows can survive.

    `copies` is set on every surviving row, so a collapsed group can say how
    many messages it stands for. Nothing is hidden silently.
    """
    rows = list(rows)
    if not rows:
        return []
    rank = rank or (lambda row: (row.get("score") or 0.0, row.get("received_at") or ""))
    where = group_ids(rows)

    dropped: set[Any] = set()
    if drop_groups is not None:
        dropped = {where[row["id"]] for row in rows if drop_groups(row)}

    best: dict[Any, dict[str, Any]] = {}
    counts: dict[Any, int] = {}
    for row in rows:
        group = where[row["id"]]
        counts[group] = counts.get(group, 0) + 1
        if eligible is not None and not eligible(row):
            continue
        if group not in best or rank(row) > rank(best[group]):
            best[group] = row

    out = []
    for row in rows:                      # caller's order is the caller's to keep
        group = where[row["id"]]
        if group in dropped or best.get(group) is not row:
            continue
        row["copies"] = counts[group]
        out.append(row)
    return out
