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
import json
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


# A reply prefix, repeated: "RE: Re: Fwd:", "답장: 회신:", and the tags mail
# gateways put in front of them ("[EXTERNAL] RE:", "[외부] 회신:").
_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|sv|답장|회신|전달|返信|转发|回复)\s*(?:\[\d+\])?\s*[:：]\s*)+", re.I)
_GATEWAY_TAG = re.compile(r"^\s*\[(?:external|ext|external sender|외부|외부메일|외부 메일|caution)\]\s*", re.I)
_MIN_KEY_WEIGHT = 12         # "Question", "Hello", "Re: hi" do not identify a thread
# Enough on its own to name one thread: a long subject, or one carrying an id
# or a [TAG]. Anything shorter ("Meeting request", "Quick question") joins a
# thread only when the reply also went to someone in it.
_SPECIFIC_WEIGHT = 28
# An id (a run of digits that is not a bare year) or a [TAG].
_SPECIFIC_MARK = re.compile(r"\b(?!(?:19|20)\d{2}\b)\d{4,}\b|\[[^\]]+\]")


def _clean_subject(subject: str | None) -> str:
    text = unicodedata.normalize("NFC", subject or "")
    while True:
        cut = _GATEWAY_TAG.sub("", _PREFIX.sub("", text))
        if cut == text:
            break
        text = cut
    return " ".join(text.casefold().split())


def _weight(text: str) -> int:
    # Hangul and CJK carry about twice the information per character.
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def subject_key(subject: str | None) -> str | None:
    """A hash of the subject with reply prefixes, gateway tags, case and
    spacing removed, or None when what is left is too short to tell one thread
    from another."""
    text = _clean_subject(subject)
    if _weight(text) < _MIN_KEY_WEIGHT:
        return None
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _specific(subject: str | None) -> bool:
    text = _clean_subject(subject)
    return _weight(text) >= _SPECIFIC_WEIGHT or bool(_SPECIFIC_MARK.search(text))


def _answered(email: dict[str, Any], received: datetime, me: str,
              sent_after: dict[str, list[tuple]] | None) -> bool:
    """The user sent mail in this thread after it arrived. A reply with a
    specific subject is enough (Outlook gives each reply branch its own
    Thread-Index root). A reply with a generic subject, or a forward (sending
    the ask on to a friend is not answering it), also needs to have gone to
    someone on this mail."""
    key = subject_key(email.get("subject"))
    if not key or not sent_after or key not in sent_after:
        return False
    people = ({(email.get("from_address") or "").lower()} | _addresses(email)) - {me, ""}
    specific = _specific(email.get("subject"))
    for entry in sent_after[key]:
        at, recipients = entry[0], entry[1]
        forward = bool(entry[2]) if len(entry) > 2 else False
        when = _when(at)
        if when is None or when <= received:
            continue
        if (specific and not forward) or recipients & people:
            return True
    return False


def is_forward(subject: str | None) -> bool:
    text = _GATEWAY_TAG.sub("", unicodedata.normalize("NFC", subject or ""))
    return bool(re.match(r"^\s*(?:fw|fwd|전달|转发|轉寄)\s*[:：]", text, re.I))


def _addresses(email: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for col in ("to_recipients", "cc_recipients"):
        value = email.get(col) or "[]"
        try:
            items = json.loads(value) if isinstance(value, str) else list(value)
        except ValueError:
            items = []
        for it in items:
            addr = it.get("address") if isinstance(it, dict) else it
            if addr:
                out.add(str(addr).lower())
    return out


# Notices that say they cannot be answered. Anchored to the notice wording, so
# "please do not reply all" or "don't respond to the vendor yet" in a person's
# ask do not match.
_NO_REPLY = re.compile(  # noqa: E501
    r"\b(?:please\s+)?(?:do\s+not|don't|pls\s+do\s+not)\s+(?:reply|respond)\s+(?:directly\s+)?to\s+this\s+"
    r"(?:e-?mail|message|address|notification|mailbox)\b|"
    r"\b(?:this|the)\s+(?:mailbox|inbox|e-?mail\s+address|address)\s+is\s+(?:unmonitored|not\s+monitored)\b|"
    r"\bunmonitored\s+(?:mailbox|inbox|e-?mail\s+address|account)\b|"
    r"(?:^|[.!?]\s+|\n)\s*this\s+is\s+an\s+(?:automated|automatic|auto-generated)\s+(?:message|e-?mail|notification)\b|"
    r"\bthis\s+(?:e-?mail|message|notification)\s+(?:was|is|has\s+been)\s+(?:automatically\s+generated|sent\s+automatically)\b|"
    # Korean notices name the mail itself ("본 메일은 발신 전용입니다", "이 메일에는
    #회신하지 마십시오"); a person's "회신이 불가하면 알려주세요" is an ask.
    r"발신\s*전용|(?:본|이)\s*(?:메일|이메일|e-?mail)[^.\n]{0,20}(?:회신|답장)\s*(?:하지\s*마|이\s*불가|되지\s*않)|"
    r"返信しないで|送信専用|请勿回复|請勿回覆", re.I)
# A greeting to nobody in particular, on the opening line only (a forwarded
# "Dear all" further down is someone else's broadcast).
_GENERIC_GREETING = re.compile(
    r"^\W*(?:dear|hi|hello|hey|greetings)\s+(?:user|users|customers?|applicants?|valued\s+\w+|"
    r"sir\s+or\s+madam|sir/madam)\s*(?:[,.!:]|$)|"
    r"^\W*(?:안녕하세요[,.!\s]*)?(?:회원님들|고객님)", re.I)
# "Dear all" and "Hi everyone" are a group ask when a small group is on the
# mail; sent to the user alone (Bcc) or to a crowd, it is a broadcast.
_GROUP_GREETING = re.compile(r"^\W*(?:dear|hi|hello|hey)\s+(?:all|everyone|everybody|students?|colleagues|"
                             r"members?|participants?)\s*(?:[,.!:]|$)|"
                             r"^\W*(?:안녕하세요[,.!\s]*)?(?:학생\s*)?여러분", re.I)
_PLACEHOLDER = re.compile(
    r"html (?:compatible|capable) (?:e-?mail )?(?:viewer|reader|client)|"
    r"view this (?:e-?mail|message) in (?:your|a) (?:web )?browser|"
    r"이 메일이 (?:잘 )?보이지 않", re.I)
_NEWSLETTER = re.compile(r"\bnewsletter\b|뉴스레터|ニュースレター|通讯|電子報", re.I)
def _opening_lines(text: str, n: int = 3) -> list[str]:
    """The first non-empty lines. A template often puts a logo line or a
    title ("HKUST Ucalendar") above its "Dear USER,"."""
    return [line for line in text.splitlines() if line.strip()][:n]


def _first_line(text: str) -> str:
    lines = _opening_lines(text, 1)
    return lines[0] if lines else ""


def _broadcast(email: dict[str, Any], text: str, me: str) -> bool:
    """A mail that reached the user directly but is not asking them."""
    first = _first_line(text)
    if _NO_REPLY.search(text) or any(_GENERIC_GREETING.match(l) for l in _opening_lines(text)):
        return True
    if _GROUP_GREETING.match(first):
        count = len(_addresses(email) | {me})
        if count <= 1 or count > 10:
            return True
    subject = email.get("subject") or ""
    if _NEWSLETTER.search(subject) and not _PREFIX.match(subject):
        return True
    if len(text) < 300 and _PLACEHOLDER.search(text):
        return True
    # Deliberately no "addressed to someone else by name" rule. Four review
    # rounds showed a greeting cannot be read reliably without knowing every
    # name the user goes by ("Jordan, thanks. Can you send me the file?" is
    # still an ask on the user). On Danny's mailbox the one such mail sat in a
    # thread he had answered, which the sent log already clears.
    return False


def _asks(email: dict[str, Any], bucket: str | None, me: str = "") -> str | None:
    """The plain reason this email asks something of the user, or None."""
    subject = email.get("subject") or ""
    if _PREFIX.match(_GATEWAY_TAG.sub("", subject)):
        subject = ""    # a reply's subject repeats the old ask
    text = cues.strip_quoted(email.get("body_text") or email.get("body_preview") or "")
    text = _SIGNATURE.split(text, maxsplit=1)[0]
    text = _URL.sub(" ", text)
    if _broadcast(email, text, me):
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
           sent_after: dict[str, list[tuple]] | None = None) -> str | None:
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
    if _answered(email, received, me, sent_after):
        return None              # answered in this thread, in some branch
    return _asks(email, bucket, me)


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
                     sent_after=sent)
        if why:
            out.append({"id": r["id"], "subject": r["subject"], "from_name": r["from_name"],
                        "from_address": r["from_address"], "received_at": r["received_at"],
                        "is_read": r["is_read"], "bucket": r["bucket"], "reason": why})
    return out
