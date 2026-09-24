"""Provider-agnostic contract, shared prompts, and tolerant JSON parsing.

Every provider gets the same prompt and must return the same shape, so swapping
Copilot for Claude for a local model changes one setting and nothing else.
"""
from __future__ import annotations

from .. import relevance

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

VALID_BUCKETS = {"action", "fyi", "noise"}

# How much of a body the model gets to see. The opening is where intent lives,
# so it is always sent whole; the rest is sampled for dates (see _body_for_prompt).
_MAX_BODY_HEAD = 2000
_MAX_BODY_TAIL = 1600      # total budget for date-bearing excerpts after the head
_DATE_WINDOW = 260         # characters kept either side of a date found later on

# Written-out and numeric dates, the forms that actually carry deadlines in mail.
# Deliberately not matching bare "30" or "3rd month" -- a number is not a date,
# and widening this to catch them would keep every page of every newsletter.
_DATE_IN_BODY = re.compile(
    r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}"
    r"|\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r"|\d{1,2}[/.]\d{1,2}[/.]\d{2,4}"
    r"|\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일)",
    re.IGNORECASE,
)


def _body_for_prompt(body: str) -> str:
    """Send the opening whole, then the parts further down that carry a date.

    A flat character cut is fine for judging what a message is about and wrong
    for reading obligations out of it. HTML tables arrive here flattened -- every
    column heading first, then every value -- so a deadline table puts its labels
    near the top and its dates hundreds of characters below them. Cutting at a
    fixed offset delivered "FYP Progress Report (Deadline: end of 3rd month)"
    while withholding the "30 August 2026" that answered it, and the model then
    did exactly as it was told: no date you can point at, no task. The email
    looked like a model failure and was a truncation.

    So: the head in full, then windows around each later date, in order, until
    the tail budget runs out. A message with no dates past the head costs the
    same as it did before."""
    body = body or ""
    if len(body) <= _MAX_BODY_HEAD:
        return body

    head, rest = body[:_MAX_BODY_HEAD], body[_MAX_BODY_HEAD:]
    windows: list[tuple[int, int]] = []
    for match in _DATE_IN_BODY.finditer(rest):
        start = max(0, match.start() - _DATE_WINDOW)
        end = min(len(rest), match.end() + _DATE_WINDOW)
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))   # merge overlaps
        else:
            windows.append((start, end))

    kept: list[str] = []
    budget = _MAX_BODY_TAIL
    for start, end in windows:
        if budget <= 0:
            break
        chunk = rest[start : start + min(end - start, budget)]
        budget -= len(chunk)
        kept.append(chunk)

    if not kept:
        return head
    return head + "\n[... trimmed; the dated lines further down follow ...]\n" + "\n[...]\n".join(kept)


MAX_TASKS_PER_EMAIL = 5
_MAX_TASK_TITLE = 120


@dataclass
class ExtractedTask:
    """One thing the recipient personally has to do, read out of the body.

    This is the difference between "this email has a date on it" and "you owe
    someone a progress report by the 30th". A single message routinely carries
    several -- a presentation date, a report deadline, a slides upload -- and
    the one `deadline` field can only ever hold one of them."""
    title: str
    due_date: str


@dataclass
class Classification:
    email_id: str
    bucket: str = "fyi"
    deadline: str | None = None
    rationale: str = ""
    matched: list[str] = field(default_factory=list)
    category: str = ""
    # What the email SAYS, for someone who has not opened it. Deliberately
    # not `rationale`: that explains why the app ranked the mail where it
    # did, which is a sentence about the app. This one is about the mail.
    summary: str = ""
    tasks: list[ExtractedTask] = field(default_factory=list)


class ProviderUnavailable(RuntimeError):
    """Raised when a provider cannot run at all (no key, not signed in, offline).
    The caller falls back to structural scoring and shows the 'AI unavailable'
    badge rather than blocking the UI.

    `key` and `vars` are how the failure reaches the screen in the reader's
    language. The message stays English and stays useful -- it goes to logs and
    to anyone reading a traceback -- but a failure the app *recognises* also
    names itself with a translation key, and the UI words it.

    This is the third time the rule has had to be applied: the briefing's
    `note_key`, the ranking reason's codes, and now this. **Text built in the
    backend is text in one language**, and it ends up sitting in the middle of
    a Korean sentence. A failure with no key -- something genuinely unexpected
    -- still renders its message, which is the right answer for a string
    nobody anticipated.
    """

    def __init__(self, message: str, *, key: str | None = None,
                 vars: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.key = key
        self.vars = vars or {}


@dataclass
class CheckResult:
    """What Settings' "test connection" learned.

    Iterable, so every existing `ok, detail = await provider.check()` and every
    test that unpacks two values keeps working. A provider that has nothing
    structured to add can still return a plain tuple.
    """

    ok: bool
    detail: str
    key: str | None = None
    vars: dict[str, Any] = field(default_factory=dict)
    models: list[str] = field(default_factory=list)

    def __iter__(self):
        return iter((self.ok, self.detail))


class LLMProvider(ABC):
    name: str = "base"
    model: str = ""

    @abstractmethod
    async def complete(self, system: str, user: str) -> str:
        """Single-turn text in, text out. The only thing a provider must implement."""

    async def check(self) -> tuple[bool, str]:
        try:
            await self.complete("Reply with the single word: ok", "ping")
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            return False, str(exc)

    async def classify_batch(self, emails: list[dict[str, Any]], priorities: list[str]) -> list[Classification]:
        if not emails:
            return []
        raw = await self.complete(CLASSIFY_SYSTEM, build_classify_prompt(emails, priorities))
        return parse_classifications(raw, [e["id"] for e in emails])

    async def summarize(
        self, emails: list[dict[str, Any]], priorities: list[str], language: str = "en"
    ) -> tuple[str, list[AgendaItem]]:
        """Returns (headline, agenda items). Items reference real email ids so
        every line in the briefing is something the user can open and tick off."""
        if not emails:
            return "Nothing in the inbox needs you today.", []
        raw = await self.complete(
            DIGEST_SYSTEM, build_digest_prompt(emails, priorities, language)
        )
        return parse_agenda(raw, [e["id"] for e in emails])


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------

CLASSIFY_SYSTEM = """You are the triage half of an executive assistant reading someone else's inbox.

For each email decide exactly one bucket:
- "action"  - the recipient personally must do, decide, answer or attend something.
- "fyi"     - genuinely useful to know, but nothing is required of the recipient.
- "noise"   - newsletters, marketing, automated notifications, receipts, and anything
              they would only ever look up later. Never mark something "noise" just
              because it is long or automated if it carries a deadline for them.

Also extract a deadline as YYYY-MM-DD when the email states or clearly implies one
(a due date, a meeting date, an RSVP cut-off, "by Friday"). Use null otherwise.
Resolve relative dates against the stated current date.

Then extract "tasks": the concrete things THIS RECIPIENT personally has to do,
each with its own date. This is the important part, and it is not the same as the
deadline field. One email often carries several separate obligations -- "submit
the progress report by 30 August", "present on 12 September", "upload slides the
day before" -- and each is its own line on their to-do list.

Rules for tasks:
- Write each title as an instruction to the user, starting with a verb, naming the
  thing: "Submit the Co-op progress report", not "Progress report" and not
  "Reminder about the progress report". Max 12 words.
- Only include something the recipient must DO. An event they are merely told
  about, a date in someone else's schedule, a deadline that has already passed for
  a group they are not in -- none of those are tasks.
- Every task needs a real date you can point at in the email. If the email says a
  thing is due but never says when, leave it out.
- Never invent or infer a date to make a task fit. Half a task is worse than none.
- Tables arrive flattened: every column heading in a run, then the row of values
  in the same order underneath. So a deadline schedule reads as a list of labels
  followed by a list of dates. Match them up by position -- the 2nd date belongs
  to the 2nd label -- rather than treating the labels as undated. Only do this
  when the counts line up; if they do not, take no date at all.
- Deadlines given as a rule ("end of the 3rd month") count only when the email
  also states the date that rule works out to. Do not compute one yourself.
- At most 5 per email. Empty list is the right answer for most mail, and always
  the right answer for newsletters and marketing.

List which of the user's active priorities the email relates to, using their exact
wording. Empty list if none.

Also give one "category" from exactly this list, describing what KIND of thing the
email is. This is independent of the bucket: an exam notice can be "fyi" and a
hackathon invitation can be "action".
- "exam"          tests, quizzes, grades, results, invigilation
- "coursework"    assignments, submissions, projects, labs, reports
- "announcement"  course or LMS notices, timetable changes, class admin
- "career"        recruiting, internships, co-op, employers, career services
- "event"         talks, seminars, socials, campus happenings
- "competition"   hackathons, contests, calls for application, prizes
- "admin"         registry, fees, ID cards, visas, housing, enrolment
- "service"       IT tickets, system notices, password and account mail
Use "" if genuinely none of them fit. Do not invent a category outside this list.

Finally write "summary": one plain sentence, at most 20 words, saying what this email
TELLS the recipient -- the fact, the change, the offer, the date. It is read by someone
who has not opened their mail yet, so it has to stand on its own: never "this email" or
"the sender", and never a restatement of the subject line, which is printed beside it.
Write it in the same language the email is written in. Use "" when the subject already
says everything there is to say.

Reply with a JSON array and nothing else. One object per email, same order as given:
[{"id": "<id>", "bucket": "action|fyi|noise", "deadline": "YYYY-MM-DD or null",
  "tasks": [{"title": "<verb-first, max 12 words>", "due": "YYYY-MM-DD"}],
  "matched": ["priority", ...], "category": "<one of the list above, or \"\">",
  "summary": "<max 20 words, or \"\">",
  "rationale": "<max 15 words>"}]"""

DIGEST_SYSTEM = """You write one short morning briefing for a busy person, in the voice of
a trusted assistant who has already read everything.

The briefing is an actionable checklist, not prose: every line the user sees is
tied to a specific email they can open and tick off. So return the *ids* of the
emails that matter today, each with a one-line reason, plus a single headline
sentence for the whole day.

Rules:
- The window is what matters. Prefer things due TODAY or within the next few
  days, then things due later in the window. An item whose deadline has already
  passed belongs here only if it passed very recently and can still be caught;
  something overdue by a week is not what "today" means, however important it
  once was.
- Within the same urgency, order by how much it matters to this person.
- At most 7 items. Leave out anything that does not need them today.
- Each note is at most 14 words, names concrete people and dates, and says what
  the user must actually DO. Not "an email about X" -- "confirm the room by 5pm".
- The headline is one sentence summing up the day. If nothing is urgent, say so.
- Never invent an id. Only use ids from the list given.

Reply with JSON and nothing else:
{"headline": "<one sentence>", "items": [{"id": "<id>", "note": "<max 14 words>"}]}"""


def _fmt_email(email: dict[str, Any], index: int) -> str:
    body = _body_for_prompt(email.get("body_text") or email.get("body_preview") or "")
    to = email.get("to_recipients") or "[]"
    cc = email.get("cc_recipients") or "[]"
    return (
        f"--- EMAIL {index} ---\n"
        f"id: {email['id']}\n"
        f"from: {email.get('from_name','')} <{email.get('from_address','')}>\n"
        f"to: {to}\ncc: {cc}\n"
        f"received: {email.get('received_at','')}\n"
        f"importance: {email.get('importance','normal')}   attachments: {bool(email.get('has_attachments'))}\n"
        f"subject: {email.get('subject','')}\n"
        f"body:\n{body}\n"
    )


def build_classify_prompt(emails: list[dict[str, Any]], priorities: list[str]) -> str:
    prio = "\n".join(f"- {p}" for p in priorities) or "- (none set yet)"
    blocks = "\n".join(_fmt_email(e, i + 1) for i, e in enumerate(emails))
    return (
        f"Current date: {date.today().isoformat()}\n\n"
        f"The user's active priorities right now:\n{prio}\n\n"
        f"Classify these {len(emails)} emails.\n\n{blocks}"
    )


LANGUAGE_NAMES = {"en": "English", "ko": "Korean", "zh": "Chinese", "ja": "Japanese"}


def build_digest_prompt(
    emails: list[dict[str, Any]], priorities: list[str], language: str = "en"
) -> str:
    prio = "\n".join(f"- {p}" for p in priorities) or "- (none set yet)"
    lines = []
    today = date.today()
    for e in emails:
        # The model is told how far off each deadline is, not only its date.
        # Asking it to prefer "today or this week" while handing it bare ISO
        # dates makes it do calendar arithmetic it is bad at, on the one field
        # the instruction turns on.
        deadline = ""
        if e.get("deadline"):
            try:
                days = (date.fromisoformat(e["deadline"]) - today).days
                when = ("due today" if days == 0
                        else f"due in {days}d" if days > 0
                        else f"{-days}d OVERDUE")
                deadline = f" | {when} ({e['deadline']})"
            except ValueError:
                deadline = f" | due {e['deadline']}"
        lines.append(
            f"id: {e['id']}\n"
            f"[{e.get('bucket','fyi')}{deadline}] {e.get('subject','')} "
            f"-- from {e.get('from_name') or e.get('from_address')}: "
            f"{(e.get('body_text') or e.get('body_preview') or '')[:400]}"
        )
    # Headline and notes are interface text written for this user, so they follow
    # the UI language. Subjects and senders are quoted from their own mail and
    # must come back exactly as they arrived.
    lang = LANGUAGE_NAMES.get(language, "English")
    return (
        f"Today is {date.today().isoformat()}.\n"
        f"Write the headline and every note in {lang}. "
        f"Do not translate email subjects or sender names -- quote them verbatim.\n\n"
        f"The user's active priorities:\n{prio}\n\n"
        f"Mail in play:\n" + "\n\n".join(lines)
    )


@dataclass
class AgendaItem:
    email_id: str
    note: str = ""


def parse_agenda(raw: str, expected_ids: list[str]) -> tuple[str, list[AgendaItem]]:
    """Map the model's chosen ids back onto real emails.

    Ids that are not in the list we asked about are dropped rather than shown.
    A checklist row that opens nothing is worse than a missing row -- and a
    hallucinated id would do exactly that.
    """
    data = extract_json(raw)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object with headline and items")

    headline = str(data.get("headline") or "").strip()[:400]
    allowed = set(expected_ids)
    items: list[AgendaItem] = []
    seen: set[str] = set()

    for entry in data.get("items") or []:
        if not isinstance(entry, dict):
            continue
        email_id = str(entry.get("id") or "").strip()
        if email_id not in allowed or email_id in seen:
            continue
        seen.add(email_id)
        items.append(AgendaItem(email_id=email_id, note=str(entry.get("note") or "").strip()[:200]))

    return headline, items


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def extract_json(raw: str) -> Any:
    """Models wrap JSON in prose or fences more often than anyone admits.
    Try the whole string, then a fenced block, then the outermost bracket pair."""
    if raw is None:
        raise ValueError("empty response")
    text = raw.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass

    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except ValueError:
            pass

    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except ValueError:
                continue
    raise ValueError(f"no JSON found in model response: {text[:200]!r}")


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean_deadline(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if value.lower() in ("null", "none", "n/a", ""):
        return None
    if not _DATE_RE.match(value):
        return None
    try:
        # Shape alone is not enough: a model will happily emit 2026-13-45,
        # which would render as a due date and score as nothing.
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def parse_classifications(raw: str, expected_ids: list[str]) -> list[Classification]:
    """Map the model's array back onto the emails we asked about.

    Matches by id where the model echoed one, and falls back to positional
    order otherwise -- but never invents a result for an email that got no
    answer, so an email is retried next pass instead of silently mislabelled.
    """
    data = extract_json(raw)
    if isinstance(data, dict):
        for key in ("results", "emails", "classifications"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        raise ValueError("expected a JSON array of classifications")

    by_id: dict[str, dict] = {}
    positional: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        if item_id in expected_ids:
            by_id[item_id] = item
        else:
            positional.append(item)

    out: list[Classification] = []
    for index, email_id in enumerate(expected_ids):
        item = by_id.get(email_id)
        if item is None:
            if index < len(positional):
                item = positional[index]
            else:
                continue
        bucket = str(item.get("bucket") or "").strip().lower()
        matched = item.get("matched") or []
        # Validated against the taxonomy, not trusted. An unknown category is
        # dropped to "" rather than kept, because a category the app does not
        # know is a feature nothing will ever learn a weight for -- it would
        # sit in the vector as a permanent zero and look like data.
        category = str(item.get("category") or "").strip().lower()
        if category not in relevance.CATEGORIES:
            category = ""
        out.append(
            Classification(
                email_id=email_id,
                bucket=bucket if bucket in VALID_BUCKETS else "fyi",
                deadline=_clean_deadline(item.get("deadline")),
                rationale=str(item.get("rationale") or "")[:300],
                matched=[str(m) for m in matched if m] if isinstance(matched, list) else [],
                category=category,
                summary=str(item.get("summary") or "").strip()[:300],
                tasks=parse_tasks(item.get("tasks")),
            )
        )
    return out


def parse_tasks(raw: Any) -> list[ExtractedTask]:
    """Turn the model's task list into something safe to put on a calendar.

    A task with no usable date is dropped rather than filed under today: a
    to-do that silently appears on the wrong day is worse than one that never
    appears, because the user will trust it."""
    if not isinstance(raw, list):
        return []
    out: list[ExtractedTask] = []
    seen: set[tuple[str, str]] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        due = _clean_deadline(entry.get("due") or entry.get("due_date") or entry.get("deadline"))
        if not title or not due:
            continue
        title = " ".join(title.split())[:_MAX_TASK_TITLE]
        key = (title.lower(), due)
        if key in seen:
            continue
        seen.add(key)
        out.append(ExtractedTask(title=title, due_date=due))
        if len(out) >= MAX_TASKS_PER_EMAIL:
            break
    return out
