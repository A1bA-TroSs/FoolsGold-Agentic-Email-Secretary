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
  5. it arrived in the last WINDOW_DAYS days;
  6. the user has not sent mail in the same thread since (any branch: the
     sent log matches the normalised subject, because Outlook's Thread-Index
     gives each reply branch its own root);
  7. it is not a broadcast wearing a direct address: "do not reply" notices,
     generic greetings ("Dear USER"), newsletters, HTML-only placeholders, or
     an ask whose opening line names someone else.

Rules 6 and 7 come from Danny's real mailbox (2026-11-03), where all seven
listed emails were one of these.

Apple Mail sets the answered flag when the user replies, so a reply in Mail
clears the row on the next sync. Outlook mail carries no answered flag in what
the app syncs; it is shown too and stays until the user marks it done
(Danny, 2026-11-03).

F6's morning report reads `owed()` so both lists agree.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from . import cues, db, priority

WINDOW_DAYS = 14
# The verdicts the app treats as dismissed (App.jsx DISMISSED, plus not relevant).
_CLOSED = ("done", "not_relevant", "not_important")

# A "?" inside a link (`...?usp=sharing`) asks nothing.
_URL = re.compile(r"(?:https?://|www\.)\S+|<[^>\s]+>", re.I)
# A signature starts at "-- " or at a sign-off standing alone on its line
# ("Thanks,", "Best regards", "감사합니다."). A sentence that merely starts
# with "Thanks for ..." is body text, and the ask below it still counts.
_SIGNATURE = re.compile(
    r"\n(?:--[ \t]*|(?:best|best regards|kind regards|warm regards|regards|thanks|thank you|"
    r"many thanks|thanks again|cheers|sincerely|sent from my [^\n]*|감사합니다|고맙습니다)[ \t]*[,.!]?[ \t]*)(?:\n|$)",
    re.I)
# Asks written without a question mark: "Can you send the slides by Friday."
# and the polite Korean question ending "가능하실까요." / "괜찮으신가요."
# "Please find attached" and "please see below" hand something over; they ask nothing.
_EN_ASK = re.compile(r"\b(?:can|could|would|will) you\b|\bplease\b(?!\s+(?:find|see|note)\b)|"
                     r"\blet me know\b", re.I)
# Bare 가요 is also a statement ("내일 회사에 가요."), so only 신가요/인가요/는가요.
_KO_QUESTION = re.compile(r"(?:까요|나요|[신인는]가요|습니까|실지요|는지요)\s*[.?？!]?\s*(?:$|\n)", re.M)


# A reply prefix, repeated: "RE: Re: Fwd:", "답장: 회신:", "[EXTERNAL] RE:".
_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|sv|답장|회신|전달|返信|转发|回复)\s*(?:\[\d+\])?\s*[:：]\s*)+", re.I)
_MIN_KEY_CHARS = 12          # "Question", "Hello", "Re: hi" do not identify a thread


def subject_key(subject: str | None) -> str | None:
    """A hash of the subject with reply prefixes, case and spacing removed, or
    None when what is left is too short to tell one thread from another."""
    text = unicodedata.normalize("NFC", subject or "")
    while True:
        cut = _PREFIX.sub("", text)
        if cut == text:
            break
        text = cut
    text = " ".join(text.casefold().split())
    if len(text) < _MIN_KEY_CHARS:
        return None
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# Mail that says itself it cannot be answered.
_NO_REPLY = re.compile(
    r"\b(?:do not|don't|please do not|pls do not) (?:reply|respond)\b|\bunmonitored\b|"
    r"\bno[- ]reply (?:address|mailbox|email)\b|\bthis is an automated (?:message|email|notification)\b|"
    r"\bautomatically generated\b|회신하지\s*마|회신\s*하지\s*마|답장하지\s*마|발신\s*전용|"
    r"회신이\s*불가|返信しないで|送信専用|请勿回复|請勿回覆", re.I)
# A greeting to nobody in particular.
_GENERIC_GREETING = re.compile(
    r"^\W*(?:dear|hi|hello|hey|greetings)\s+(?:user|users|all|everyone|everybody|students?|"
    r"colleagues|members?|customers?|participants?|applicants?|team|valued \w+|sir or madam|"
    r"sir/madam|friends?)\s*(?:[,.!:]|$)|^\W*(?:안녕하세요[,.!\s]*)?(?:여러분|회원님들|학생 여러분|고객님)", re.I | re.M)
_PLACEHOLDER = re.compile(
    r"html (?:compatible|capable) (?:e-?mail )?(?:viewer|reader|client)|"
    r"view this (?:e-?mail|message) in (?:your|a) (?:web )?browser|"
    r"이 메일이 (?:잘 )?보이지 않", re.I)
_NEWSLETTER = re.compile(r"\bnewsletter\b|뉴스레터|ニュースレター|通讯|電子報", re.I)
# "Dion," / "Hi Dion," / "Dear Prof. Kim," alone on the opening line.
_OPENING = re.compile(r"^\s*(?:(?i:hi|hello|dear|hey)\s+)?(?:(?i:prof|dr|mr|ms|mrs)\.?\s+)?"
                      r"([A-Z][A-Za-z'-]{1,20})\s*[,:!]\s*$")
_OPENING_KO = re.compile(r"^\s*([가-힣]{2,4})\s*(?:님|씨|선생님|교수님)\s*[,.!]?")
_NOT_NAMES = {"thanks", "thank", "hi", "hello", "dear", "hey", "sorry", "ok", "okay", "yes",
              "no", "sure", "update", "note", "fyi", "reminder", "all", "team", "everyone",
              "greetings", "morning", "afternoon", "evening", "cheers", "regards", "noted",
              "attention", "important", "urgent", "hello all"}


def _opening_name(text: str) -> str | None:
    for line in text.splitlines():
        if line.strip():
            m = _OPENING.match(line) or _OPENING_KO.match(line)
            if m and m.group(1).casefold() not in _NOT_NAMES:
                return m.group(1)
            return None
    return None


def _broadcast(email: dict[str, Any], text: str, names: set[str]) -> bool:
    """A mail that reached the user directly but is not asking them."""
    if _NO_REPLY.search(text) or _GENERIC_GREETING.search(text):
        return True
    if _NEWSLETTER.search(email.get("subject") or ""):
        return True
    if len(text) < 300 and _PLACEHOLDER.search(text):
        return True
    named = _opening_name(text)
    # Compared only against a name in the same script: a user known as "Danny
    # Park" may well be greeted "윤성님," and that is still them.
    hangul = lambda w: bool(re.search(r"[가-힣]", w))
    same_script = {n for n in names if hangul(n) == hangul(named or "")}
    if named and same_script and not any(named.casefold() in n or n in named.casefold()
                                         for n in same_script):
        return True              # "Dion, please follow up" asks Dion
    return False


def _name_tokens(name: str | None) -> set[str]:
    return {t.casefold() for t in re.split(r"[\s,.]+", name or "") if len(t) > 1}


def _asks(email: dict[str, Any], bucket: str | None, names: set[str] | None = None) -> str | None:
    """The plain reason this email asks something of the user, or None."""
    subject = email.get("subject") or ""
    if re.match(r"^\s*(?:re|fw|fwd|답장|회신|전달)\s*[:：]", subject, re.I):
        subject = ""    # a reply's subject repeats the old ask
    text = cues.strip_quoted(email.get("body_text") or email.get("body_preview") or "")
    text = _SIGNATURE.split(text, maxsplit=1)[0]
    text = _URL.sub(" ", text)
    if _broadcast(email, text, names or set()):
        return None
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
           now: datetime | None = None,
           user_name: str | None = None,
           sent_after: dict[str, str] | None = None) -> str | None:
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
    key = subject_key(email.get("subject"))
    replied = _when((sent_after or {}).get(key)) if key else None
    if replied is not None and replied > received:
        return None              # answered in this thread, in some branch
    return _asks(email, bucket, _name_tokens(user_name))


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
    user_name = db.get_setting("user_name", "") or ""
    sent = db.sent_after()
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
                     muted=bool(r["muted"]), snooze_until=r["snooze_until"], now=now,
                     user_name=user_name, sent_after=sent)
        if why:
            out.append({"id": r["id"], "subject": r["subject"], "from_name": r["from_name"],
                        "from_address": r["from_address"], "received_at": r["received_at"],
                        "is_read": r["is_read"], "bucket": r["bucket"], "reason": why})
    return out
