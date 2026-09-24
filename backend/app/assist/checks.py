"""Deterministic pre-send checks. No model, no network, no settings.

Phase P0 of `claude/assist-plan.md`. Everything here is a rule a person could
verify by reading it, which is the point: the model half of the review can be
wrong about tone, but *this* half must never be wrong about whether the word
"attached" appears in a message with no attachment.

Three rules shape the whole file, and all three come from measured evidence:

1. **Precision over coverage.** SSL warnings that were mostly false positives
   were ignored by 90% of users even on a bank site (Sunshine et al., USENIX
   Security 2009). A check that fires on mail the user would have sent anyway
   is worse than no check, so every rule here is tuned against the user's own
   sent mail and drops out when unsure.
2. **Only the user's own words.** Quoted originals and signatures are removed
   before anything is examined. Half the plausible false alarms -- the sender
   saying "첨부합니다", someone else's casual line inside a quote -- are quoted
   text, and they belong to whoever wrote them.
3. **No prose.** An issue carries a `kind` and a span, never an English
   sentence: the UI translates. English prose reaching a Korean screen is a
   defect this project has already shipped once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence


@dataclass(frozen=True)
class Issue:
    kind: str          # what is wrong; the UI holds the wording for each kind
    field: str         # "subject" | "body"
    start: int         # span within `subject` or within the message body
    end: int
    evidence: str = "" # a short quote, so the panel can show what it means
    extra: str = ""    # rule-specific payload (a suggestion, a matched word)


# --------------------------------------------------------------------------
# 1. the user's own words
# --------------------------------------------------------------------------

# How this app's own compose quotes a parent, plus what other clients send.
_ATTRIBUTION = re.compile(
    r"^\s*(?:"
    r"On\s.{0,120}\swrote:\s*$"                      # Apple Mail, Gmail, ours
    r"|.{0,80}\s(?:작성|씀|보냄)\s*:?\s*$"             # Korean clients
    r"|-{2,}\s*(?:Original Message|원본 메시지|원본 메일)\s*-{2,}\s*$"
    r"|_{5,}\s*$"                                    # Outlook's rule
    r"|From:\s.{0,200}$"                             # Outlook header block
    r")",
    re.IGNORECASE | re.MULTILINE,
)
# RFC 3676 §4.3: "-- " alone on a line opens a signature.
_SIGNATURE = re.compile(r"^--\s*$", re.MULTILINE)


def own_text(body: str) -> str:
    """The message minus quoted material and signature, padded so that every
    offset still points at the same character in the original body.

    Padding rather than cutting is deliberate: a span this module reports has
    to be highlightable in the textarea the user is looking at, and an index
    into a shortened copy points at the wrong word."""
    body = body or ""
    keep = list(body)

    def blank(start: int, end: int) -> None:
        for i in range(start, min(end, len(keep))):
            if keep[i] != "\n":
                keep[i] = " "

    cut = len(body)
    for pattern in (_ATTRIBUTION, _SIGNATURE):
        found = pattern.search(body)
        if found and found.start() < cut:
            cut = found.start()
    blank(cut, len(body))

    # Quoted lines above the attribution too: a top-post reply to a reply.
    for match in re.finditer(r"^[ \t]*>.*$", body[:cut], re.MULTILINE):
        blank(match.start(), match.end())
    return "".join(keep)


# --------------------------------------------------------------------------
# 2. sentences, with their offsets
# --------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"[.!?。！？…]+[\s\"')\]]*|\n+")


def sentences(text: str) -> list[tuple[int, int, str]]:
    """(start, end, text) for each sentence-ish run. Offsets are into `text`."""
    out: list[tuple[int, int, str]] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        end = match.end()
        chunk = text[start:end]
        if chunk.strip():
            out.append((start, end, chunk))
        start = end
    tail = text[start:]
    if tail.strip():
        out.append((start, len(text), tail))
    return out


# --------------------------------------------------------------------------
# 3. subject
# --------------------------------------------------------------------------

# Reply and forward prefixes, in the four languages the UI speaks plus the ones
# that reach a Korean mailbox. Matched only as a whole subject, never stripped
# from one -- compose/message.py owns prefix handling.
_BARE_SUBJECT = re.compile(
    r"^(?:\s*(?:re|답장|회신|답신|回复|回覆|返信|rv|ref|aw|sv|antw|fwd?|전달|转发|転送)\s*[::\]]*)*\s*$",
    re.IGNORECASE,
)
_NO_SUBJECT = re.compile(r"^\s*[\(\[]?\s*(?:제목\s*없음|no subject|untitled|무제)\s*[\)\]]?\s*$",
                         re.IGNORECASE)


def check_subject(subject: str) -> list[Issue]:
    text = subject or ""
    if _NO_SUBJECT.match(text) or _BARE_SUBJECT.match(text):
        return [Issue(kind="subject_missing", field="subject", start=0, end=len(text),
                      evidence=text.strip())]
    return []


# --------------------------------------------------------------------------
# 4. an attachment that was promised and is not there
# --------------------------------------------------------------------------

_PROMISE = re.compile(
    r"(첨부\s*(?:파일|자료|문서|드립니다|드려요|합니다|했습니다|하였습니다|해\s*드립니다|해요|함)"
    r"|파일을?\s*(?:첨부|동봉)"
    r"|attach(?:ed|ing|ment)s?\b"
    r"|enclosed\b|enclosure\b)",
    re.IGNORECASE,
)
# Someone else's attachment, or one that already exists elsewhere: "보내주신
# 첨부파일", "the attached file you sent", "첨부파일 확인했습니다". These are the
# false alarms this rule would otherwise produce on real replies.
_THEIRS = re.compile(
    r"(보내주신|주신|보내신|받은|첨부해\s*주신|첨부해주신|말씀하신"
    r"|you\s+(?:sent|attached)|your\s+attach|previously\s+attached"
    r"|아래|위\s*에|지난(?:번)?|이전)",
    re.IGNORECASE,
)
_RECEIVED = re.compile(
    r"(확인\s*(?:했|하였|드렸|드립니다|했습니다)|잘\s*받았|받았습니다|검토\s*(?:했|하였|중)"
    r"|감사(?:합니다|드립니다|해요)"
    r"|(?:i|we)\s+(?:have\s+)?(?:received|reviewed|got)|thank(?:s| you)\s+for)",
    re.IGNORECASE,
)
# Future tense: the promise is for a later message, not this one.
_LATER = re.compile(
    r"(보내\s*드리겠|보내겠|드리겠습니다|전달\s*드리겠|추후|나중에|내일|다음\s*(?:주|달|메일)"
    r"|will\s+(?:send|attach|share|follow)|follow\s*up|later\s+today|tomorrow|next\s+week)",
    re.IGNORECASE,
)


def check_attachment(body_own: str, has_attachment: bool) -> list[Issue]:
    if has_attachment:
        return []
    out: list[Issue] = []
    for start, end, chunk in sentences(body_own):
        promise = _PROMISE.search(chunk)
        if not promise:
            continue
        if _THEIRS.search(chunk) or _RECEIVED.search(chunk) or _LATER.search(chunk):
            continue
        out.append(Issue(kind="attachment_missing", field="body",
                         start=start + promise.start(), end=start + promise.end(),
                         evidence=chunk.strip()[:120], extra=promise.group(0)))
        break                      # one is a warning; three is nagging
    return out


# --------------------------------------------------------------------------
# 5. 존댓말 / 반말 mixed inside one Korean message
# --------------------------------------------------------------------------

_HANGUL = re.compile(r"[가-힣]")

# 합쇼체 and 해요체. Both are polite; mixing *those two* is ordinary Korean and
# is not flagged -- only polite against plain is.
# NOTE: `ㅂ니다` never appears as a standalone jamo in real text -- it is fused
# into the preceding syllable (하 + ㅂ니다 = 합니다), so a regex written with the
# jamo matches nothing. The first version of this file had exactly that bug and
# silently classed every 합니다 sentence as "no register". Match the syllables.
_POLITE = re.compile(
    r"(?:니다|니까|십시오|세요|셔요|시죠|어요|아요|에요|예요|해요|지요|죠|네요|군요"
    r"|는데요|거든요|까요|나요|데요|요)"
    r"\s*[.!?~…]*\s*$"
)
# 해체 / 해라체 / 한다체. `다` only when it is not the tail of `니다`.
_PLAIN = re.compile(
    r"(?:(?<!니)다|[아어여]라|자|냐|니|는가|은가|군|구나|네|지|거든|잖아|더라|더군|야|어|아|여|걸)"
    r"\s*[.!?~…]*\s*$"
)
# Endings that are neither: a noun, a list item, a heading, an English tail.
_NEUTRAL_TAIL = re.compile(r"[가-힣]\s*$")


def _register(chunk: str) -> str:
    """"polite" | "plain" | "" for one sentence, by its final word."""
    text = chunk.strip().strip("\"'”’)]}»›")
    if not text or not _HANGUL.search(text):
        return ""
    # Quoted speech and cited lines carry someone else's register.
    if text.startswith(("\"", "“", "'", "‘", "「", "『", ">")):
        return ""
    if _POLITE.search(text):
        return "polite"
    # A bullet, a heading or a fragment has no register to disagree with.
    if re.match(r"^\s*(?:[-*•▪◦·]|\d+[.)]|[가-힣]\.)\s", text):
        return ""
    if text.rstrip().endswith((":", "：", "-", "—")):
        return ""
    # A plain ending only counts when the line really ends -- with a full stop,
    # a question mark or an exclamation. Korean nouns routinely end in the same
    # syllables the plain endings use ("메시지", "탐지", "편지"), and a heading
    # or a list line that happens to end in one is not 반말. Measured on real
    # Korean mail, requiring final punctuation removed every false alarm this
    # rule produced without losing a single true one.
    if not text.rstrip("\"'”’)]}»›").endswith((".", "!", "?", "…", "。", "！", "？")):
        return ""
    if _PLAIN.search(text) and _NEUTRAL_TAIL.search(text.rstrip(".!?~… ")):
        return "plain"
    return ""


def check_honorific(body_own: str) -> list[Issue]:
    """Flag a Korean message that mixes polite and plain endings.

    Deliberately narrow. "This is too casual for your professor" is a judgement
    and belongs to the model half; "this message is polite in five sentences
    and plain in one" is a fact about the text, and a fact is what a
    deterministic check is allowed to assert."""
    polite: list[tuple[int, int, str]] = []
    plain: list[tuple[int, int, str]] = []
    for start, end, chunk in sentences(body_own):
        kind = _register(chunk)
        if kind == "polite":
            polite.append((start, end, chunk))
        elif kind == "plain":
            plain.append((start, end, chunk))
    if not polite or not plain:
        return []
    # The minority register is the odd one out, and it is what gets shown.
    odd = plain if len(plain) <= len(polite) else polite
    start, end, chunk = odd[0]
    stripped = chunk.strip()
    offset = chunk.index(stripped) if stripped in chunk else 0
    return [Issue(kind="honorific_mixed", field="body",
                  start=start + offset, end=start + offset + len(stripped),
                  evidence=stripped[:120],
                  extra=f"polite={len(polite)} plain={len(plain)}")]


# --------------------------------------------------------------------------
# 6. English spelling, through an injected linter
# --------------------------------------------------------------------------

# A callable that takes text and returns (start, end, kind, evidence) tuples.
# Injected rather than imported so this module stays pure, testable and
# offline: the Harper bridge lives next door and is optional at runtime.
Linter = Callable[[str], Sequence[tuple[int, int, str, str]]]


def english_only(text: str) -> str:
    """The same text with every Hangul-bearing sentence blanked out.

    An English checker must not see Korean, and Harper's own `isolateEnglish`
    option is not a safe way to arrange that: measured, it dropped an ordinary
    English sentence ("Dear Prof, I recieve teh document...") entirely, finding
    zero issues where three were present. Blanking sentences *here* is a rule
    with one sentence of explanation, and it keeps every offset intact."""
    keep = list(text)
    for start, end, chunk in sentences(text):
        if _HANGUL.search(chunk):
            for i in range(start, min(end, len(keep))):
                if keep[i] != "\n":
                    keep[i] = " "
    return "".join(keep)


def check_english(body_own: str, linter: Linter | None) -> list[Issue]:
    if linter is None or not body_own.strip():
        return []
    text = english_only(body_own)
    if not text.strip():
        return []
    try:
        found = linter(text)
    except Exception:                                  # noqa: BLE001
        return []                                      # a checker that breaks says nothing
    return [Issue(kind="spelling_en", field="body", start=start, end=end,
                  evidence=evidence[:120], extra=label)
            for start, end, label, evidence in found]


# --------------------------------------------------------------------------
# 7. the whole check
# --------------------------------------------------------------------------

MAX_ISSUES = 5     # the panel's cap; see the plan's §0.2 on warning fatigue


def check(subject: str, body: str, *, has_attachment: bool = False,
          linter: Linter | None = None) -> list[Issue]:
    """Every deterministic issue in one outgoing message, worst first."""
    text = own_text(body)
    issues = (check_subject(subject)
              + check_attachment(text, has_attachment)
              + check_honorific(text)
              + check_english(text, linter))
    order = {"subject_missing": 0, "attachment_missing": 1, "honorific_mixed": 2,
             "spelling_en": 3}
    issues.sort(key=lambda i: (order.get(i.kind, 9), i.start))
    return issues[:MAX_ISSUES]


def as_dicts(issues: Iterable[Issue]) -> list[dict]:
    return [{"kind": i.kind, "field": i.field, "start": i.start, "end": i.end,
             "evidence": i.evidence, "extra": i.extra} for i in issues]
