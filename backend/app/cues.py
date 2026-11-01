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

# A completed or past-tense form ("제출 완료되었습니다", "마감되었습니다") is a
# confirmation, not a request, so the bare nouns refuse to match before one.
_DONE = r"(?![^.\n]{0,10}?(?:완료|되었|됐|되셨|하였|했|처리되))"
KO_ACTION = re.compile(
    r"(마감" + _DONE + r"|제출" + _DONE + r"|기한|회신|답장\s*(?:부탁|요망|바랍니다)|"
    r"확인\s*(?:요청|부탁|바랍니다|해\s*주)|승인\s*(?:요청|부탁|바랍니다)|서명|"
    r"신청\s*(?:기간|마감|바랍니다)|필수|긴급|요청" + _DONE + r"|독촉|리마인드|"
    r"참석\s*여부|미납|납부\s*(?:기한|마감|안내)|까지\s*(?:제출|회신|신청|납부)|"
    r"(?:검토|작성|업로드|수정|회신|확인|제출)\s*(?:을|를)?\s*"
    r"(?:부탁\s*드|부탁해|해\s*주시기|하여\s*주시기|해\s*주세요|해주세요|바랍니다)|"
    r"올려\s*주(?:세요|시기)|보내\s*주(?:세요|시기))"
)

# `(광고)` / `[광고]` is the legally required advertising marker in Korea
# (정보통신망법 제50조), so it is the most reliable noise cue a Korean mailbox
# has. Senders put it at the front as the law asks, but some put it at the end.
KO_NOISE_SUBJECT = re.compile(
    r"([\(\[]\s*광고\s*[\)\]]|수신\s*거부|뉴스레터|소식지|웹진|\d+\s*월호|주간\s*소식|"
    r"특가|쿠폰|할인|영수증|결제\s*완료|주문\s*(?:확인|완료)|배송\s*(?:시작|완료|안내))"
)

# English marketing and bulk subjects. One copy, read by priority and relevance.
EN_NOISE_SUBJECT = re.compile(
    r"\b(unsubscribe|newsletter|webinar|sale|% ?off|deal|promo|digest|"
    r"weekly round[- ]?up|invoice|receipt|order (?:confirm|ship))\b", re.I
)


def is_marketing(subject: str) -> bool:
    subject = subject or ""
    return bool(EN_NOISE_SUBJECT.search(subject) or KO_NOISE_SUBJECT.search(subject))


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
    found: list[tuple[int, date]] = []

    def first(pattern, build):
        for m in pattern.finditer(text):
            d = build(m)
            if d:
                found.append((m.start(), d))
                return

    first(_KO_YMD, lambda m: _safe(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    first(_DOTTED, lambda m: _safe(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    first(_KO_MD, lambda m: _next_occurrence(int(m.group(1)), int(m.group(2)), today))
    first(_KO_SHORT, lambda m: _next_occurrence(int(m.group(1)), int(m.group(2)), today))
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
    # Earliest in the text wins; on a tie the more specific pattern (added
    # first) wins, because min() keeps the first of equal keys.
    return min(found, key=lambda item: item[0])


# --------------------------------------------------------------------------
# always-important mail types
# --------------------------------------------------------------------------

ALWAYS_IMPORTANT = {
    "exam": re.compile(
        r"(\bmid-?terms?\b|\bfinal exams?\b|\bexams?\b|\bquiz(?:zes)?\b|"
        r"시험|중간고사|기말고사|퀴즈)", re.I),
    "payment": re.compile(
        r"(\btuition\b|\bpayment (?:due|deadline|reminder|overdue)\b|\bfees? (?:are )?due\b|"
        r"등록금|수강료|납부|미납|고지서)", re.I),
    "ticket": re.compile(
        r"(\bboarding pass\b|\be-?ticket\b|\bitinerary\b|\bbooking confirm|"
        r"\breservation confirm|\bflight (?:confirm|change|cancel)|"
        r"탑승권|항공권|전자\s*티켓|예매\s*(?:완료|확인)|예약\s*(?:확정|완료|확인))", re.I),
    # Calendar invitations as Google and Outlook title them. The bare word
    # 초대 is not enough: product launches and LinkedIn use it too.
    "invite": re.compile(
        r"(^\s*(?:updated\s+)?invitation:|\bmeeting request\b|"
        r"^\s*(?:업데이트된\s*)?초대(?:장)?\s*:|일정\s*(?:초대|공유)|회의\s*(?:초대|요청))", re.I),
}


def always_important(email: dict[str, Any], is_marketing_mail: bool | None = None) -> str | None:
    """The kind of must-see mail this is, or None.

    Read from the SUBJECT only. A body that mentions an exam or a payment is
    usually a newsletter or a chat ("I'm overdue on laundry"); the notices that
    matter name the kind in their subject. Marketing is never must-see,
    because "토익 시험 30% 할인" is an advert that contains the word for exam.
    """
    subject = email.get("subject") or ""
    if is_marketing_mail if is_marketing_mail is not None else is_marketing(subject):
        return None
    for kind, pattern in ALWAYS_IMPORTANT.items():
        if pattern.search(subject):
            return kind
    return None
