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

KO_ACTION = re.compile(
    r"(마감|제출|기한|회신|답장\s*(?:부탁|요망|바랍니다)|확인\s*(?:요청|부탁|바랍니다|해\s*주)|"
    r"승인|서명|신청\s*(?:기간|마감|바랍니다)|필수|긴급|요청|독촉|리마인드|"
    r"참석\s*여부|미납|납부\s*(?:기한|마감|안내)|까지\s*(?:제출|회신|신청|납부))"
)

# `(광고)` / `[광고]` at the front of the subject is the legally required
# advertising marker in Korea (정보통신망법 제50조), so it is the most reliable
# noise cue a Korean mailbox has.
KO_NOISE_SUBJECT = re.compile(
    r"(^\s*[\(\[]\s*광고\s*[\)\]]|수신\s*거부|뉴스레터|특가|쿠폰|할인|"
    r"영수증|결제\s*완료|주문\s*(?:확인|완료)|배송\s*(?:시작|완료|안내))"
)

# --------------------------------------------------------------------------
# Korean dates
# --------------------------------------------------------------------------

_KO_YMD = re.compile(r"(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_DOTTED = re.compile(r"(?<![\d.])(20\d{2})\s*[.]\s*(\d{1,2})\s*[.]\s*(\d{1,2})(?![\d])")
_KO_MD = re.compile(r"(?<!\d)(\d{1,2})\s*월\s*(\d{1,2})\s*일")
# 11/3(월) -- slash dates are only trusted with the weekday beside them, which
# is how Korean mail writes them; a bare 1/2 is too often a fraction or a score.
_KO_SLASH = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})\s*\(\s*[월화수목금토일]\s*\)")
_KO_RELATIVE = re.compile(r"(오늘|내일|모레)\s*(?:중\s*)?까지")
_KO_WEEKDAY = re.compile(
    r"(이번\s*주|다음\s*주|차주|금주)?\s*([월화수목금토일])요일[^.\n]{0,12}?까지"
)
_KO_WEEKDAYS = {d: i for i, d in enumerate("월화수목금토일")}
_KO_RELATIVE_DAYS = {"오늘": 0, "내일": 1, "모레": 2}


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


def korean_deadline(text: str, today: date) -> tuple[int, date] | None:
    """The first Korean-style date in `text`, with where it was found.

    Returns (position, date) so the caller can choose between this and an
    English match by which comes first in the text.
    """
    found: list[tuple[int, date]] = []

    for m in _KO_YMD.finditer(text):
        d = _safe(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            found.append((m.start(), d))
            break
    for m in _DOTTED.finditer(text):
        d = _safe(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            found.append((m.start(), d))
            break
    for m in _KO_MD.finditer(text):
        d = _next_occurrence(int(m.group(1)), int(m.group(2)), today)
        if d:
            found.append((m.start(), d))
            break
    for m in _KO_SLASH.finditer(text):
        d = _next_occurrence(int(m.group(1)), int(m.group(2)), today)
        if d:
            found.append((m.start(), d))
            break
    m = _KO_RELATIVE.search(text)
    if m:
        found.append((m.start(), today + timedelta(days=_KO_RELATIVE_DAYS[m.group(1)])))
    m = _KO_WEEKDAY.search(text)
    if m:
        target = _KO_WEEKDAYS[m.group(2)]
        week = re.sub(r"\s", "", m.group(1) or "")
        if week in ("다음주", "차주"):
            monday_next = today - timedelta(days=today.weekday()) + timedelta(days=7)
            found.append((m.start(), monday_next + timedelta(days=target)))
        else:
            # "금요일까지" on a Friday means today, not a week from now.
            found.append((m.start(), today + timedelta(days=(target - today.weekday()) % 7)))

    if not found:
        return None
    # A full year-month-day date beats a span that only overlaps it, so on a tie
    # in position the earlier pattern (more specific) wins.
    return min(found, key=lambda item: item[0])


# --------------------------------------------------------------------------
# always-important mail types
# --------------------------------------------------------------------------

ALWAYS_IMPORTANT = {
    "exam": re.compile(
        r"(\bmid-?terms?\b|\bfinal exams?\b|\bexams?\b|\bquiz(?:zes)?\b|"
        r"시험|중간고사|기말고사|퀴즈)", re.I),
    "payment": re.compile(
        r"(\btuition\b|\bpayment (?:due|deadline|reminder|overdue)\b|\bfees? due\b|"
        r"\boverdue\b|등록금|수강료|납부|미납|고지서)", re.I),
    "ticket": re.compile(
        r"(\bboarding pass\b|\be-?ticket\b|\bitinerary\b|\bbooking confirm|"
        r"\breservation confirm|\bflight (?:confirm|change|cancel)|"
        r"탑승권|항공권|전자\s*티켓|예매\s*(?:완료|확인)|예약\s*(?:확정|완료|확인))", re.I),
    "invite": re.compile(
        r"(^\s*(?:updated\s+)?invitation:|\bmeeting request\b|\binvited you\b|"
        r"초대|회의\s*요청|일정\s*(?:초대|공유))", re.I),
}


def always_important(email: dict[str, Any], is_marketing: bool = False) -> str | None:
    """The kind of must-see mail this is, or None.

    Read from the subject first and the opening of the body second. Marketing
    is excluded by the caller's own noise-subject test, because "토익 시험 30%
    할인" is an advert that happens to contain the word for exam.
    """
    if is_marketing:
        return None
    subject = email.get("subject") or ""
    body = (email.get("body_text") or email.get("body_preview") or "")[:600]
    for kind, pattern in ALWAYS_IMPORTANT.items():
        if pattern.search(subject):
            return kind
    for kind, pattern in ALWAYS_IMPORTANT.items():
        if kind == "invite":
            continue  # "invite" in a body is mostly "you are invited to our webinar"
        if pattern.search(body):
            return kind
    return None
