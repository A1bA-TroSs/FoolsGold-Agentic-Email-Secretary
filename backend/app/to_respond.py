"""Replies you owe (roadmap A1, "To respond").

The pain the user research named is following through, not reading: replies
put off until they are lost. This is a view over mail already stored, built
from the helpers the ranker already uses, and it changes no ranking.

An email is owed a reply when all of these hold:
  1. it was written to the user directly (To, not only cc, a list or bcc);
  2. it is from a person: not the user, not a no-reply or bulk sender, not
     marketing, not a muted sender;
  3. it asks for something: a question in the new text (quoted replies cut
     off), an action word in English or Korean, or the stored bucket is action;
  4. it is not answered, and the user has not marked it done or not relevant;
  5. it arrived in the last WINDOW_DAYS days.

Apple Mail sets the answered flag when the user replies, so a reply in Mail
clears the row on the next sync. Outlook mail carries no answered flag in what
the app syncs; it is shown too and stays until the user marks it done
(Danny, 2026-11-03).

F6's morning report reads `owed()` so both lists agree.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from . import cues, db, priority

WINDOW_DAYS = 14
_CLOSED = ("done", "not_relevant")

# A "?" inside a link (`...?usp=sharing`) asks nothing.
_URL = re.compile(r"(?:https?://|www\.)\S+|<[^>\s]+>", re.I)
# A signature starts at "-- " or a common sign-off line; links and phone
# numbers below it are not part of the ask.
_SIGNATURE = re.compile(r"\n(?:--\s*\n|(?:best|regards|thanks|cheers|sent from my)\b[^\n]*\n)", re.I)
# Asks written without a question mark: "Can you send the slides by Friday."
# and the polite Korean question ending "가능하실까요." / "괜찮으신가요."
_EN_ASK = re.compile(r"\b(?:can|could|would|will) you\b|\bplease\b|\blet me know\b", re.I)
_KO_QUESTION = re.compile(r"(?:까요|나요|가요|습니까|ㄹ까요|실지요|는지요)\s*[.?？!]?\s*(?:$|\n)", re.M)


def _asks(email: dict[str, Any], bucket: str | None) -> str | None:
    """The plain reason this email asks something of the user, or None."""
    subject = email.get("subject") or ""
    if re.match(r"^\s*(?:re|fw|fwd|답장|회신|전달)\s*[:：]", subject, re.I):
        subject = ""    # a reply's subject repeats the old ask
    text = cues.strip_quoted(email.get("body_text") or email.get("body_preview") or "")
    text = _SIGNATURE.split(text, maxsplit=1)[0]
    text = _URL.sub(" ", text)
    if any(q in part for part in (_URL.sub(" ", subject), text) for q in ("?", "？")):
        return "question"
    if _KO_QUESTION.search(text):
        return "question"
    if cues.asks_ko(f"{subject}\n{text}") or cues.ko_action(f"{subject}\n{text}"):
        return "request"
    if cues.EN_ACTION.search(f"{subject}\n{text}") or _EN_ASK.search(text):
        return "request"
    if bucket == "action":
        return "action"
    return None


def reason(email: dict[str, Any], me: str, *, bucket: str | None = None,
           verdict: str | None = None, muted: bool = False,
           snooze_until: str | None = None,
           now: datetime | None = None) -> str | None:
    """Why this email is owed a reply ("question", "request", "action"), or
    None when it is not. Each condition is checked on its own so a test can
    pin each one."""
    me = (me or "").lower()
    sender = (email.get("from_address") or "").lower()
    if not me or sender == me or muted:
        return None
    if not priority._addressed_directly(email, me):
        return None
    if cues.NOISE_SENDER.search(sender) or cues.is_marketing_mail(email):
        return None
    if email.get("is_answered") or verdict in _CLOSED:
        return None
    now = now or datetime.now(timezone.utc)
    if verdict == "snoozed":
        # Hidden until the snooze ends, as in every other list.
        until = _when(snooze_until)
        if until is None or until > now:
            return None
    received = _when(email.get("received_at"))
    if received is None or received < now - timedelta(days=WINDOW_DAYS):
        return None
    return _asks(email, bucket)


def _when(value: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def owed(me: str | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Every email owed a reply, newest first, each with its `reason`."""
    if me is None:
        from .pipeline import user_address
        me = user_address()
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=WINDOW_DAYS + 1)).isoformat()
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT e.id, e.subject, e.from_name, e.from_address, e.to_recipients, "
            "       e.cc_recipients, e.received_at, e.is_read, e.is_answered, "
            "       e.body_text, e.body_preview, c.bucket, f.verdict, f.snooze_until, "
            "       (e.from_address IN (SELECT address FROM muted_senders)) AS muted "
            "FROM emails e "
            "LEFT JOIN classifications c ON c.email_id = e.id "
            "LEFT JOIN feedback f ON f.email_id = e.id "
            "WHERE COALESCE(e.is_answered, 0) = 0 AND e.received_at >= ? "
            "ORDER BY e.received_at DESC", (since,))]
    out = []
    for r in rows:
        why = reason(r, me, bucket=r["bucket"], verdict=r["verdict"],
                     muted=bool(r["muted"]), snooze_until=r["snooze_until"], now=now)
        if why:
            out.append({"id": r["id"], "subject": r["subject"], "from_name": r["from_name"],
                        "from_address": r["from_address"], "received_at": r["received_at"],
                        "is_read": r["is_read"], "bucket": r["bucket"], "reason": why})
    return out
