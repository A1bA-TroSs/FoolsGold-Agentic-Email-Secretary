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
_DONE = r"(?![^.\n]{0,10}?(?:완료|되었|됐|되어|됨|된|돼|되셨|하였|했|하신|해\s*주신|해\s*주셔서|(?<!면)(?<!면\s)감사|처리되|지난|지나|끝))"
KO_ACTION = re.compile(
    r"(마감" + _DONE + r"(?=\s*(?:일|기한|시간|일시|안내|임박|까지|전|:|：|\d|\())|"
    r"제출" + _DONE + r"(?=\s*(?:기한|마감|일|요청|바랍|부탁|하세요|하시기|해\s*주|까지|방법|안내|:|：))|(?<!유통)(?<!사용)기한(?!\s*연장)" + _DONE + r"|"
    r"회신\s*(?:부탁|요망|바랍니다|요청|주시|해\s*주|기한)|"
    r"답장\s*(?:부탁|요망|바랍니다)|확인\s*(?:요청|부탁|바랍니다|해\s*주)|"
    r"승인\s*(?:요청|부탁|바랍니다)|서명\s*(?:요청|부탁|해\s*주|바랍니다|필요)|"
    r"신청\s*(?:기간|마감|바랍니다)" + _DONE + r"|필수\s*(?:제출|참석|확인|이수|서류|응답)|[\(\[]\s*필수\s*[\)\]]|긴급|"
    r"요청\s*(?:드립니다|드려요|드리오니|합니다|하오니|바랍니다)|"
    r"(?:자료|검토|확인|회신|제출|참석|서명|승인)\s*요청|독촉|리마인드|"
    r"참석\s*여부|미납|납부\s*(?:기한|마감|안내)|까지\s*(?:제출|회신|신청|납부)|"
    r"(?:검토|작성|업로드|수정|회신|확인|제출)\s*(?:을|를)?\s*"
    r"(?:부탁\s*드|부탁해|해\s*주시기|하여\s*주시기|해\s*주세요|해주세요|바랍니다)|"
    r"올려\s*주(?:세요|시기)|보내\s*주(?:세요|시기)|신청\s*하(?:세요|시기)|답변\s*요청|"
    r"(?:납부|참석|답변|결재|회신|제출|신청|확인|서명|검토|송부|전달|입금|의견|이수)\s*(?:하여|해)?\s*(?:주시기|하시기)?\s*"
    r"(?:바랍니다|부탁\s*드립니다|부탁드려요)|알려\s*주(?:세요|시기)|(?:신청|입금|송부)\s*해\s*주(?:세요|시기)|"
    r"(?:참여|응답|작성)\s*(?:부탁|바랍니다|해\s*주)|"
    r"(?:응시|등록|출석|참석|제출|신청|이수|작성|완료|납부)\s*하시기\s*바랍니다|"
    r"(?:하여|해)\s*주십시오|(?:공유|완료|출석|응시|등록|작성|제출|참석|납부)\s*해\s*주(?:세요|십시오))"
)

# The legally required advertising marker in Korea (정보통신망법 제50조) is
# `(광고)`. Senders write it with every kind of bracket, full-width ones and
# spaces inside (`（광고）`, `【광고】`, `<광 고>`), at the front or the end.
_AD_MARK = r"[\(\[（【<〈［]\s*(?:광\s*고|AD)\s*[\)\]）】>〉］]"
KO_NOISE_SUBJECT = re.compile(
    r"(" + _AD_MARK + r"|수신\s*거부|뉴스레터|소식지|웹진|\d+\s*월호|주간\s*소식|"
    r"영수증|결제\s*완료|주문\s*(?:확인|완료)|배송\s*(?:시작|완료|안내))",
    re.I,
)

# Korean promotional words. Like PROMO_SUBJECT, read only by the floors:
# "등록금 납부 마감 임박 안내" and "선착순 기숙사 신청" are real school mail, so
# these words must never put mail INTO noise, only keep an advert from being
# lifted out of it.
KO_PROMO_SUBJECT = re.compile(
    r"(특가|쿠폰|할인|마감\s*임박|선착순|얼리\s*버드|무료\s*(?:특강|체험|증정|세미나|제공)|"
    r"웨비나|[\[\(]\s*이벤트\s*[\]\)]|최저가)"
)

# English marketing and bulk subjects, exactly as develop had them in both
# priority.py and relevance.py. One copy now; the words are unchanged, so the
# English noise bucket is unchanged.
EN_NOISE_SUBJECT = re.compile(
    r"\b(unsubscribe|newsletter|webinar|sale|% ?off|deal|promo|digest|"
    r"weekly round[- ]?up|invoice|receipt|order (?:confirm|ship))\b", re.I
)

# English action cues, likewise moved here unchanged from develop's two copies.
EN_ACTION = re.compile(
    r"\b(action required|urgent|asap|deadline|due|reminder|rsvp|respond|reply|"
    r"confirm|approve|approval|sign|submit|complete|required|request(?:ed)?|"
    r"overdue|final notice|last chance to)\b", re.I
)

# Wider promotional vocabulary. Read ONLY by the must-see floors, never by the
# noise bucket: "Q3 sales numbers" must stay where develop put it, but an
# "Exam season: 3 months free" offer must not be lifted out of noise.
PROMO_SUBJECT = re.compile(
    r"(\b(?:newsletters|webinars|sales|deals|promos?|promotions?|free trial|"
    r"\d+ months? free|limited[- ]time|discount|coupons?)\b|\bsave \d+\s*%?|\d+\s*% off)",
    re.I,
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
    r"신청\s*(?:기간|마감|안내)|이의\s*신청|접수\s*(?:기간|마감)|(?:철회|변경|정정)\s*기간|"
    r"과제|\bassignment\b|수강\s*(?:신청|철회|정정)|[\(\[]\s*필수\s*[\)\]]|이수\s*안내)",
    re.I,
)


# "참여해 주셔서 감사합니다", "확인해 주신 분께" -- thanks for something done.
_THANKS_AFTER = re.compile(r"[^.\n]{0,12}?(?:주셔서|주신|감사)")
# "제출해 주시면 감사하겠습니다" -- grateful IF you do it: the commonest polite
# request ending, not thanks for something done.
_GRATEFUL_IF = re.compile(r"[^.\n]{0,12}?면\s*(?:정말\s*|대단히\s*|매우\s*)?감사(?:하겠|드리겠)")


# Announcement closings: "많은 참여 부탁드립니다", "자세한 내용은 홈페이지에서
# 확인 부탁드립니다". Every notice ends this way and develop kept them in fyi,
# so a participation or look-it-up ask in such a sentence is not a request.
_COURTESY_VERB = re.compile(r"(참여|응답|확인|참고|이용|방문|관심)")
_COURTESY_CONTEXT = re.compile(
    r"(많은|적극적?인?|여러분의|관심과|성원|자세한\s*(?:내용|사항|안내)|자세히|세부\s*(?:내용|사항)|"
    r"홈\s*페이지|웹\s*사이트|누리집|공지\s*사항|게시판)"
)


def _courtesy(text: str, m: re.Match) -> bool:
    if not _COURTESY_VERB.match(m.group(0)):
        return False
    start = max(text.rfind(c, 0, m.start()) for c in ".\n!?")
    return bool(_COURTESY_CONTEXT.search(text[start + 1:m.start()]))


def ko_action(text: str) -> bool:
    """A Korean request in `text`: a KO_ACTION match not followed by thanks
    and not an announcement's courtesy closing."""
    text = text or ""
    return any((_GRATEFUL_IF.match(text, m.end()) or not _THANKS_AFTER.match(text, m.end()))
               and not _courtesy(text, m)
               for m in KO_ACTION.finditer(text))


def asks_ko(text: str) -> bool:
    """A Korean request in `text`, reading only the new part of a reply.

    A body with the legal opt-out footer (수신거부) is advertising, and its
    "지금 신청하세요" is a sales line, not a request."""
    text = strip_quoted(text or "")
    if re.search(r"수신\s*거부", text):
        return False
    return ko_action(text)


_KO_SELLING = re.compile(r"(특가|쿠폰|할인|최저가|무료\s*(?:체험|증정|제공)|[\[\(]\s*이벤트\s*[\]\)])")


def is_dated_ask(email: dict[str, Any], deadline: str | None, today: date | None = None,
                 grace_days: int = 2) -> bool:
    """The mail asks for something by a date it names, and is not marketing.

    One predicate for both callers (`priority.structural_bucket` and
    `relevance.dated_ask`). The measured Canvas notice ("과제 제출 마감 안내:
    11월 3일까지" from notifications@instructure.com) is this: not one of the
    must-see kinds, but a dated request. From a system sender it needs the
    same proof as the must-see floor.
    """
    if not deadline or is_marketing_mail(email):
        return False
    try:
        due = date.fromisoformat(str(deadline)[:10])
    except (TypeError, ValueError):
        return False
    if due < (today or date.today()) - timedelta(days=grace_days):
        return False
    if NOISE_SENDER.search(email.get("from_address") or "") and not has_evidence(email):
        return False
    return bool(DATED_ASK_SUBJECT.search(email.get("subject") or ""))


def is_marketing(subject: str) -> bool:
    subject = subject or ""
    return bool(EN_NOISE_SUBJECT.search(subject) or KO_NOISE_SUBJECT.search(subject))


def is_marketing_mail(email: dict[str, Any]) -> bool:
    """Marketing for the floors' purposes: by subject (including the wider
    promo words), by a broadcast sender, or by the Korean opt-out footer
    (수신거부) that the law requires in every advertising mail."""
    subject = email.get("subject") or ""
    body = email.get("body_text") or email.get("body_preview") or ""
    # Promo vocabulary blocks the floor only for senders that are not a
    # school, government or LMS: "[마감임박] 등록금 분할납부 신청" from
    # no-reply@snu.ac.kr is a real deadline. The (광고) marker and the
    # 수신거부 footer block every sender.
    institutional = bool(INSTITUTIONAL_SENDER.search(email.get("from_address") or ""))
    promo = bool(PROMO_SUBJECT.search(subject) or KO_PROMO_SUBJECT.search(subject))
    selling = bool(PROMO_SUBJECT.search(subject) or _KO_SELLING.search(subject))
    # Urgency words (마감 임박, 선착순) are how schools write too; selling words
    # (할인, 특가, save 30%) are not, so those still block a school sender.
    return (is_marketing(subject) or (promo and not institutional) or selling
            or bool(MARKETING_SENDER.search(email.get("from_address") or ""))
            or bool(re.search(r"수신\s*거부", body)))


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
    r"(?:\(\s*[월화수목금토일]\s*\)|까지|\s*마감|\s*\d{1,2}\s*시\s*까지)"
)
# "마감 11/20 23:59", "기한: 11.20" -- a short date right after a deadline word.
# Slash only: "기한 2.5배" and "3.5 million" are numbers, not dates.
_KO_AFTER_WORD = re.compile(
    r"(?:마감|기한|제출일)\s*(?:일|일시)?\s*[:：]?\s*(\d{1,2})\s*(?:/|\.(?=\s*\d{1,2}(?!\s*(?:배|%|만|억|천|원|점)|\d|\.\d)))\s*(\d{1,2})(?![\d/]|\.\d)"
)
_KO_THIS_MONTH_END = re.compile(r"(?:이번\s*달|이달|금월)\s*말\s*까지")
# "26.11.20" -- a two-digit year, trusted only with a weekday or 까지 after it.
_KO_YY_DOTTED = re.compile(
    r"(?<![\d.])(\d{2})\.(\d{1,2})\.(\d{1,2})\.?\s*(?:\(\s*[월화수목금토일]\s*\)|까지)"
)
# develop's ISO pattern ends in \\b, which a Hangul letter does not provide,
# so "2026-11-20까지" found nothing.
_ISO_KO = re.compile(r"(?<!\d)(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)")
# "5일까지" -- a bare day of this month, or next month if it has passed.
_KO_DAY_ONLY = re.compile(r"(?<![\d월/.\s])\s*(?<!\d)(\d{1,2})\s*일\s*(?:\(\s*[월화수목금토일]\s*\)\s*)?까지")
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
    r"[^.\n]{0,8}?(?:진행된|진행했|진행되었|있었|보낸|보내드린|했던|열린|열렸|개최된|발송된|공지한|공지된|받은|드린|"
    r"했습니다|였습니다|었습니다|했어요|감사했)"
)
# "까지 휴관/휴무/점검/할인": a closure or offer lasting until a date. Nothing
# is asked of the reader by then.
_NOT_A_DUE = re.compile(
    r"[^.\n]{0,4}?(?:까지)?\s*(?:는|만|동안)?\s*(?:\S{1,4}\s*)?(?:휴관|휴무|휴강|휴진|휴업|휴교|공사|점검|운영|영업|전시|할인|세일|이벤트)"
)
_NOT_A_DUE_RANGE = re.compile(
    r"\s*(?:\(\s*[월화수목금토일]\s*\)\s*)?(?:부터|~|～|〜|–|-)[^.\n]{1,16}?까지\s*(?:는|만|동안)?\s*"
    r"(?:\S{1,4}\s*)?(?:휴관|휴무|휴강|휴진|휴업|휴교|공사|점검|운영|영업|전시|할인|세일|이벤트)"
)
# "Python 3.10까지", "평점 3.5까지", "교재 1/2까지": a version, score or share.
_NOT_A_DATE_BEFORE = re.compile(r"([A-Za-z]|평점|학점|성적|버전|교재|분량|진도|점수)\s*$")
# "지난 11월 2일", "저번 3일" -- a date introduced as the past.
_PAST_BEFORE = re.compile(r"(지난|저번|작년|전날)\s*$")
# Two dates joined as a period: "11월 1일부터 12월 15일까지", "11. 10.(화) ~ 11. 14.(토)".
_RANGE_JOIN = re.compile(r"\s*(?:\(\s*[월화수목금토일]\s*\)\s*)?(?:부터|~|～|〜|–|-)\s*")
_DEADLINE_WORD = re.compile(r"(까지|마감|기한|내로|이내|due|by)", re.I)


def _anchored(text: str, m: re.Match) -> bool:
    """A deadline word within a few characters after the date, or just before it."""
    after = text[m.end():m.end() + 12]
    before = text[max(0, m.start() - 8):m.start()]
    return bool(_DEADLINE_WORD.search(m.group(0)) or _DEADLINE_WORD.search(after)
                or _DEADLINE_WORD.search(before))


# "최대 5일까지", "총 3일까지" -- a number of days, not a day of the month.
_SPAN_BEFORE = re.compile(r"(최대|총|최장|약|최소|길게|주|하루|한\s*달)\s*$")


def _day_of_month(day: int, today: date) -> date | None:
    d = _safe(today.year, today.month, day)
    if d and d >= today:
        return d
    y, mo = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return _safe(y, mo, day)


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
    found: list[tuple[int, date, bool, int]] = []

    def first(pattern, build):
        # Every match, not only the first: "11월 1일 공지한 과제는 11월 7일까지"
        # names two dates and the deadline is the one with 까지 beside it.
        for m in pattern.finditer(text):
            if _PAST_AFTER.match(text, m.end()):
                continue  # "1/3(월) 진행된 회의록", "10월 1일에 보낸 메일"
            if _PAST_BEFORE.search(text[max(0, m.start() - 6):m.start()]):
                continue
            if _NOT_A_DUE.match(text, m.end()) or _NOT_A_DUE_RANGE.match(text, m.end()):
                continue  # "다음 주 금요일까지 휴관합니다": how long, not by when
            d = build(m)
            if d:
                found.append((m.start(), d, _anchored(text, m), m.end()))

    first(_KO_YMD, lambda m: _safe(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    first(_DOTTED, lambda m: _safe(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    first(_KO_MD, lambda m: _next_occurrence(int(m.group(1)), int(m.group(2)), today))
    first(_KO_SHORT, lambda m: None if _NOT_A_DATE_BEFORE.search(text[max(0, m.start() - 8):m.start()])
          else _next_occurrence(int(m.group(1)), int(m.group(2)), today))
    first(_KO_AFTER_WORD, lambda m: _next_occurrence(int(m.group(1)), int(m.group(2)), today))
    first(_KO_YY_DOTTED, lambda m: _safe(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))))
    first(_KO_THIS_MONTH, lambda m: _safe(today.year, today.month, int(m.group(1))))
    first(_ISO_KO, lambda m: _safe(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    first(_KO_DAY_ONLY, lambda m: None if _SPAN_BEFORE.search(text[max(0, m.start() - 4):m.start() + 1])
          else _day_of_month(int(m.group(1)), today))
    first(_KO_MONTH_END, lambda m: _month_end(int(m.group(1)), today))
    first(_KO_THIS_MONTH_END, lambda m: _month_end(today.month, today))
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

    # A date already past with no deadline word beside it is a reference to
    # something that happened ("10/30(금) 강의 녹화본"), not an overdue task.
    found = [f for f in found if f[2] or f[1] >= today]
    if not found:
        return None
    # A date with a deadline word beside it beats one without; then the
    # earliest in the text; on a tie the more specific pattern (added first).
    pos, d, _, end = min(found, key=lambda item: (not item[2], item[0]))
    # A period's deadline is its end: follow "부터" / "~" to the second date.
    join = _RANGE_JOIN.match(text, end)
    if join:
        after = [f for f in found if f[0] == join.end() and f[1] >= d]
        if after:
            pos, d = after[0][0], after[0][1]
        else:
            # "11월 10일(화)~14일(토)": the end keeps the start's month.
            day = re.match(r"(\d{1,2})\s*일", text[join.end():])
            if day:
                end_d = _safe(d.year, d.month, int(day.group(1)))
                if end_d and end_d >= d:
                    pos, d = join.end(), end_d
    return pos, d


# --------------------------------------------------------------------------
# always-important mail types
# --------------------------------------------------------------------------

ALWAYS_IMPORTANT = {
    # Bare "quiz" and 퀴즈 are left out: "Quiz: which Pokemon are you?" is the
    # common case. 시험 발송 / 시험 메일 is a test send, not an exam.
    "exam": re.compile(
        r"(\bmid-?terms?\b|\bfinal exams?\b|\bexams?\b|\bquiz\s*\d|"
        r"시험(?!\s*(?:발송|메일|용|전송|삼아))|중간고사|기말고사)", re.I),
    "payment": re.compile(
        r"(\btuition\b(?![- ]free)|\bpayment (?:due|deadline|reminder|overdue)\b|\bfees? (?:are )?due\b|"
        r"등록금|수강료|납부|미납|고지서|상환|청구\s*(?:서|금액|안내|예정)|요금|대금|결제\s*(?:예정|기한|예정일))", re.I),
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


def has_evidence(email: dict[str, Any]) -> bool:
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
    if NOISE_SENDER.search(email.get("from_address") or "") and not has_evidence(email):
        return None
    for kind, pattern in ALWAYS_IMPORTANT.items():
        if pattern.search(subject):
            if kind in ("payment", "exam") and _SETTLED.search(subject):
                continue
            return kind
    return None
