"""Text cues the no-AI path reads: Korean phrasing, and mail that is never noise.

Why a module of its own
-----------------------
`priority.py` and `relevance.py` each kept their own copy of the action and
noise word lists, and both copies were English only. On a Korean mailbox with
the default provider `none`, "과제 제출 마감 안내: 11월 3일까지" found no action
word and no deadline, and `(광고)` mail -- which Korean law requires to say so in
the subject -- was not recognised as advertising. The Korean half lives here so
both callers read the same list.

Korean has no spaces between a word and its particle (`마감일`, `제출을`), so
these patterns are substring patterns. `\\b` would miss exactly the forms that
are most common.

Always important
----------------
Some kinds of mail must never be hidden as noise however the relevance model
scores them on day one: an exam notice, a tuition or payment deadline, a
travel ticket, a calendar invitation. They often arrive from `no-reply@` or
`notifications@` addresses, which is the strongest noise cue there is, so the
floor is applied after the bucket is derived (`Axes.bucket`) rather than as one
more weighted feature that a learned weight could talk back down.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

# --------------------------------------------------------------------------
# Korean action and noise cues
# --------------------------------------------------------------------------

# A completed or past-tense form ("제출 완료되었습니다", "조기 마감되어") is a
# confirmation, not a request, so the bare nouns refuse to match before one.
_DONE = r"(?![^.\n]{0,10}?(?:완료|되었|됐|되어|됨|되셨|하였|했|하신|처리되))"
KO_ACTION = re.compile(
    r"(마감" + _DONE + r"|제출" + _DONE + r"|기한|회신(?!\s*(?:불필요|필요\s*없|하지\s*않))|"
    r"답장\s*(?:부탁|요망|바랍니다)|확인\s*(?:요청|부탁|바랍니다|해\s*주)|"
    r"승인\s*(?:요청|부탁|바랍니다)|서명\s*(?:요청|부탁|해\s*주|바랍니다|필요)|"
    r"신청\s*(?:기간|마감|바랍니다)|필수|긴급|"
    r"요청\s*(?:드립니다|드려요|드리오니|합니다|하오니|바랍니다)|"
    r"(?:자료|검토|확인|회신|제출|참석|서명|승인)\s*요청|독촉|리마인드|"
    r"참석\s*여부|미납|납부\s*(?:기한|마감|안내)|까지\s*(?:제출|회신|신청|납부)|"
    r"(?:검토|작성|업로드|수정|회신|확인|제출)\s*(?:을|를)?\s*"
    r"(?:부탁\s*드|부탁해|해\s*주시기|하여\s*주시기|해\s*주세요|해주세요|바랍니다)|"
    r"올려\s*주(?:세요|시기)|보내\s*주(?:세요|시기))"
)

# The legally required advertising marker in Korea (정보통신망법 제50조) is
# `(광고)`. Senders write it with every kind of bracket, full-width ones and
# spaces inside (`（광고）`, `【광고】`, `<광 고>`), at the front or the end.
_AD_MARK = r"[\(\[（【<〈［]\s*(?:광\s*고|AD)\s*[\)\]）】>〉］]"
KO_NOISE_SUBJECT = re.compile(
    r"(" + _AD_MARK + r"|수신\s*거부|뉴스레터|소식지|웹진|\d+\s*월호|주간\s*소식|"
    r"특가|쿠폰|할인|영수증|결제\s*완료|주문\s*(?:확인|완료)|배송\s*(?:시작|완료|안내)|"
    r"마감\s*임박|선착순|얼리\s*버드|무료\s*(?:특강|체험|증정|세미나|제공)|웨비나|"
    r"[\[\(]\s*이벤트\s*[\]\)]|최저가)",
    re.I,
)

# English marketing and bulk subjects. One copy, read by priority and relevance.
EN_NOISE_SUBJECT = re.compile(
    r"\b(unsubscribe|newsletters?|webinars?|sales?|% ?off|deals?|promos?|promotions?|digest|"
    r"weekly round[- ]?up|invoice|receipt|order (?:confirm|ship)|free trial|"
    r"\d+ months? free|limited[- ]time|save \d|discount|coupon)\b", re.I
)

# System senders: no-reply, notification and bulk addresses. Mail from them is
# noise by default; the floors below are what may lift it.
NOISE_SENDER = re.compile(
    r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|newsletter|mailer|notifications?|"
    r"marketing|updates?|alerts?|billing|receipts?|support)@", re.I
)

# Senders whose job is to sell or broadcast. The must-see floors never apply
# to them, whatever their subject says: an exam-prep advert says "exam", and a
# conference promo says "Invitation:".
MARKETING_SENDER = re.compile(
    r"(^|[._-])(marketing|newsletters?|news|promos?|promotions?|offers?|deals?|"
    r"events?|campaigns?|mailer|ads?)@", re.I
)


# What a dated request says in its SUBJECT. Stricter than the general action
# cue: "last chance to", "reminder" and "complete your profile" are how
# promotions and product mail talk, and with a date in the body they would
# otherwise be lifted out of noise.
DATED_ASK_SUBJECT = re.compile(
    r"(\b(?:due|deadline|submit|submission|action required|respond|rsvp|approve|"
    r"approval|sign(?:ature)? required|overdue|final notice)\b|"
    r"마감" + _DONE + r"|제출" + _DONE + r"|기한|회신\s*요청|미납|납부\s*(?:기한|안내)|서명\s*요청|"
    r"신청\s*(?:기간|마감|안내)|이의\s*신청|접수\s*(?:기간|마감))",
    re.I,
)


def is_marketing(subject: str) -> bool:
    subject = subject or ""
    return bool(EN_NOISE_SUBJECT.search(subject) or KO_NOISE_SUBJECT.search(subject))


def is_marketing_mail(email: dict[str, Any]) -> bool:
    """Marketing by its subject or by a broadcast sender address."""
    return (is_marketing(email.get("subject") or "")
            or bool(MARKETING_SENDER.search(email.get("from_address") or "")))


# --------------------------------------------------------------------------
# Korean dates
# --------------------------------------------------------------------------

_KO_YMD = re.compile(r"(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_DOTTED = re.compile(r"(?<![\d.])(20\d{2})\s*[.]\s*(\d{1,2})\s*[.]\s*(\d{1,2})(?![\d])")
_KO_MD = re.compile(r"(?<!\d)(\d{1,2})\s*월\s*(\d{1,2})\s*일")
# 11/3(월), 11. 20.(금), 11/20까지 -- a month/day with no year is only trusted
# with a weekday or 까지 beside it, which is how Korean mail writes deadlines;
# a bare 1/2 or 2.5 is too often a fraction, a score or a version.
_KO_SHORT = re.compile(
    r"(?<![\d/.])(\d{1,2})\s*[/.]\s*(\d{1,2})\.?\s*"
    r"(?:\(\s*[월화수목금토일]\s*\)|까지)"
)
# "마감 11/20 23:59", "기한: 11.20" -- a short date right after a deadline word.
# Slash only: "기한 2.5배" and "3.5 million" are numbers, not dates.
_KO_AFTER_WORD = re.compile(
    r"(?:마감|기한)\s*(?:일|일시)?\s*[:：]?\s*(\d{1,2})\s*/\s*(\d{1,2})(?![\d/.])"
)
# "26.11.20" -- a two-digit year, trusted only with a weekday or 까지 after it.
_KO_YY_DOTTED = re.compile(
    r"(?<![\d.])(\d{2})\.(\d{1,2})\.(\d{1,2})\.?\s*(?:\(\s*[월화수목금토일]\s*\)|까지)"
)
_KO_THIS_MONTH = re.compile(r"(?:이번\s*달|이달|금월)\s*(\d{1,2})\s*일")
_KO_MONTH_END = re.compile(r"(?<!\d)(\d{1,2})\s*월\s*말\s*까지")
_TIME = r"(?:\s*(?:오전|오후|낮|밤|저녁|아침)?\s*(?:\d{1,2}\s*시(?:\s*\d{1,2}\s*분|\s*반)?|\d{1,2}:\d{2}|자정|정오))?"
_KO_RELATIVE = re.compile(r"(오늘|내일|모레)" + _TIME + r"\s*(?:중\s*)?까지")
_KO_WEEKDAY = re.compile(
    r"(지난\s*주|저번\s*주|이번\s*주|다음\s*주|차주|금주)?\s*([월화수목금토일])요일[^.\n]{0,12}?까지"
)
_KO_WEEKDAYS = {d: i for i, d in enumerate("월화수목금토일")}
_KO_RELATIVE_DAYS = {"오늘": 0, "내일": 1, "모레": 2}

# Where a reply quotes the message before it. Dates below this line belong to
# the earlier message, not to anything this one asks.
_QUOTE_START = re.compile(
    r"^(?:>|-{2,}\s*(?:original message|원본 메시지|원래 메시지)|"
    r".{0,80}(?:님이 작성|작성:|wrote:)\s*$|보낸\s*사람\s*:|from:\s)",
    re.I | re.M,
)


def strip_quoted(text: str) -> str:
    m = _QUOTE_START.search(text or "")
    return text[:m.start()] if m else (text or "")


def _safe(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _next_occurrence(month: int, day: int, today: date) -> date | None:
    """A bare '3월 3일' is almost always the next one, as in English mail."""
    candidate = _safe(today.year, month, day)
    if candidate and candidate < today - timedelta(days=180):
        return _safe(today.year + 1, month, day)
    return candidate


# A date followed by a past-tense phrase describes what happened, not a due date.
_PAST_AFTER = re.compile(
    r"[^.\n]{0,6}?(?:진행된|진행했|진행되었|있었|보낸|보내드린|했던|열린|열렸|개최된|발송된|공지한|공지된|받은|드린)"
)
_DEADLINE_WORD = re.compile(r"(까지|마감|기한|내로|이내|due|by)", re.I)


def _anchored(text: str, m: re.Match) -> bool:
    """A deadline word within a few characters after the date, or just before it."""
    after = text[m.end():m.end() + 12]
    before = text[max(0, m.start() - 8):m.start()]
    return bool(_DEADLINE_WORD.search(m.group(0)) or _DEADLINE_WORD.search(after)
                or _DEADLINE_WORD.search(before))


def _month_end(month: int, today: date) -> date | None:
    first = _next_occurrence(month, 1, today)
    if not first:
        return None
    nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def korean_deadline(text: str, today: date) -> tuple[int, date] | None:
    """The first Korean-style date in `text`, with where it was found.

    Quoted earlier messages are cut off first. Returns (position, date).
    """
    text = strip_quoted(text)
    found: list[tuple[int, date, bool]] = []

    def first(pattern, build):
        # Every match, not only the first: "11월 1일 공지한 과제는 11월 7일까지"
        # names two dates and the deadline is the one with 까지 beside it.
        for m in pattern.finditer(text):
            if _PAST_AFTER.match(text, m.end()):
                continue  # "1/3(월) 진행된 회의록", "10월 1일에 보낸 메일"
            d = build(m)
            if d:
                found.append((m.start(), d, _anchored(text, m)))

    first(_KO_YMD, lambda m: _safe(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    first(_DOTTED, lambda m: _safe(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    first(_KO_MD, lambda m: _next_occurrence(int(m.group(1)), int(m.group(2)), today))
    first(_KO_SHORT, lambda m: _next_occurrence(int(m.group(1)), int(m.group(2)), today))
    first(_KO_AFTER_WORD, lambda m: _next_occurrence(int(m.group(1)), int(m.group(2)), today))
    first(_KO_YY_DOTTED, lambda m: _safe(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))))
    first(_KO_THIS_MONTH, lambda m: _safe(today.year, today.month, int(m.group(1))))
    first(_KO_MONTH_END, lambda m: _month_end(int(m.group(1)), today))
    first(_KO_RELATIVE, lambda m: today + timedelta(days=_KO_RELATIVE_DAYS[m.group(1)]))

    def weekday(m):
        target = _KO_WEEKDAYS[m.group(2)]
        week = re.sub(r"\s", "", m.group(1) or "")
        if week in ("지난주", "저번주"):
            return None  # a past week is history, not a deadline
        if week in ("다음주", "차주"):
            monday_next = today - timedelta(days=today.weekday()) + timedelta(days=7)
            return monday_next + timedelta(days=target)
        # "금요일까지" on a Friday means today, not a week from now.
        return today + timedelta(days=(target - today.weekday()) % 7)
    first(_KO_WEEKDAY, weekday)

    if not found:
        return None
    # A date with a deadline word beside it beats one without; then the
    # earliest in the text; on a tie the more specific pattern (added first).
    pos, d, _ = min(found, key=lambda item: (not item[2], item[0]))
    return pos, d


# --------------------------------------------------------------------------
# always-important mail types
# --------------------------------------------------------------------------

ALWAYS_IMPORTANT = {
    # Bare "quiz" and 퀴즈 are left out: "Quiz: which Pokemon are you?" is the
    # common case. 시험 발송 / 시험 메일 is a test send, not an exam.
    "exam": re.compile(
        r"(\bmid-?terms?\b|\bfinal exams?\b|\bexams?\b|\bquiz\s*\d|"
        r"시험(?!\s*(?:발송|메일|용|전송))|중간고사|기말고사)", re.I),
    "payment": re.compile(
        r"(\btuition\b|\bpayment (?:due|deadline|reminder|overdue)\b|\bfees? (?:are )?due\b|"
        r"등록금|수강료|납부|미납|고지서|결제\s*(?:예정|기한|예정일))", re.I),
    "ticket": re.compile(
        r"(\bboarding pass\b|\be-?ticket\b|\bitinerary\b|\bbooking confirm|"
        r"\breservation confirm|\bflight (?:confirm|change|cancel)|"
        r"탑승권|항공권|승차권|전자\s*티켓|예매\s*(?:완료|확인)|예약\s*(?:확정|완료|확인))", re.I),
    # Calendar invitations as Google and Outlook title them. The bare word
    # 초대 is not enough: product launches and LinkedIn use it too.
    "invite": re.compile(
        r"(^\s*(?:updated\s+)?invitation:|\bmeeting request\b|"
        r"^\s*(?:업데이트된\s*)?초대(?:장)?\s*:|일정\s*(?:초대|공유)|회의\s*(?:초대|요청))", re.I),
}

# A payment that is done, or results that are out, is a record, not a must-see.
_SETTLED = re.compile(
    r"(완료|되었습니다|처리\s*안내|received|thank you for your payment|paid\b|results?\b)", re.I
)


# Positive evidence that a system sender's must-see mail is real: a school,
# government or LMS domain, a calendar service, or a booking reference.
INSTITUTIONAL_SENDER = re.compile(
    r"@(?:[\w.-]+\.)?(?:ac\.[a-z]{2}|edu(?:\.[a-z]{2})?|go\.kr|gov(?:\.[a-z]{2})?|mil|"
    r"instructure\.com|blackboard\.com|moodle[\w.-]*|brightspace\.com|d2l\.com|"
    r"gradescope\.com|piazza\.com|edstem\.org)$|"
    r"^calendar[-_.]?notification@|@(?:calendar|resource\.calendar)\.google\.com$",
    re.I,
)
TRANSACTION_REF = re.compile(
    r"(예약\s*번호|예매\s*번호|승차권\s*번호|탑승\s*(?:게이트|시간)|편명|좌석\s*(?:번호|배정)|"
    r"\b(?:booking|reservation|confirmation|ticket|e-?ticket)\s*(?:ref(?:erence)?|number|no\.?|code|#)|"
    r"\bPNR\b|\bgate\s+[A-Z]?\d|\bflight\s+[A-Z]{2}\s?\d{2,4}\b|\b[A-Z]{2}\s?\d{3,4}\b.{0,20}\b(?:depart|dep\.?)|"
    r"\.ics\b|BEGIN:VCALENDAR)",
    re.I,
)


def _has_evidence(email: dict[str, Any]) -> bool:
    sender = email.get("from_address") or ""
    if INSTITUTIONAL_SENDER.search(sender):
        return True
    text = "%s\n%s" % (email.get("subject") or "",
                        (email.get("body_text") or email.get("body_preview") or "")[:3000])
    return bool(TRANSACTION_REF.search(text))


def always_important(email: dict[str, Any], is_marketing_mail_: bool | None = None) -> str | None:
    """The kind of must-see mail this is, or None.

    Read from the SUBJECT only. A body that mentions an exam or a payment is
    usually a newsletter or a chat ("I'm overdue on laundry"); the notices that
    matter name the kind in their subject. Marketing, by subject or by a
    broadcast sender, is never must-see: "토익 시험 30% 할인" is an advert that
    contains the word for exam.
    """
    subject = email.get("subject") or ""
    marketing = is_marketing_mail_ if is_marketing_mail_ is not None else is_marketing_mail(email)
    if marketing:
        return None
    # From a no-reply or notification address the kind word alone is how
    # promotions talk ("Exam season: 3 months free" from no-reply@quizlet), so
    # such mail also needs a school or calendar sender or a booking reference.
    if NOISE_SENDER.search(email.get("from_address") or "") and not _has_evidence(email):
        return None
    for kind, pattern in ALWAYS_IMPORTANT.items():
        if pattern.search(subject):
            if kind in ("payment", "exam") and _SETTLED.search(subject):
                continue
            return kind
    return None
