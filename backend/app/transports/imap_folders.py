"""Finding a special-use mailbox over IMAP, and filing a message in one.

Two mailboxes matter and for opposite reasons. **Sent** is where a copy goes
after the message has already left; **Drafts** is where a message goes *instead*
of leaving, for the user to send from their own client. Both are found the same
way, so the discovery is written once against RFC 6154 rather than twice against
two hard-coded names.

Nothing here is specific to one provider or one machine. RFC 6154 attributes are
the standard; the name lists exist only because the attributes are optional, and
they are localised because mailbox names are.
"""
from __future__ import annotations

import re
import time
from typing import Any, Protocol

# RFC 6154 special-use attributes. The server advertises these in its LIST
# responses; they are the only *correct* way to find these mailboxes, and they
# are optional, so they are the first way rather than the only way.
SENT_ATTRIBUTE = "\\Sent"
DRAFTS_ATTRIBUTE = "\\Drafts"

# Fallback names, used only when the server advertises no attribute.
#
# Deliberately not English-only, and deliberately not a guess at which language
# the user speaks: Gmail localises its IMAP folder names per account, so one
# user's Sent mailbox is "Sent Mail" and another's is "[Gmail]/보낸편지함",
# and the client cannot know which without looking. Matching is against the
# whole list regardless of locale.
#
# These lists will never be complete -- there are more languages than anyone
# will enumerate, and users rename folders. That is what the per-account
# override in Settings is for, and why a miss raises a message naming it
# rather than silently picking the wrong mailbox.
SENT_NAMES = (
    "sent", "sent items", "sent messages", "sent mail",
    "보낸편지함", "보낸 편지함", "보낸 메일",
    "送信済み", "已发送", "已寄郵件",
    "gesendet", "gesendete elemente", "envoyés", "éléments envoyés",
    "enviados", "elementos enviados", "posta inviata", "verzonden items",
    "отправленные", "gönderilmiş öğeler",
)

DRAFTS_NAMES = (
    "drafts", "draft",
    "임시보관함", "임시 보관함",
    "下書き", "草稿", "草稿箱",
    "entwürfe", "brouillons", "borradores", "bozze", "concepten",
    "черновики", "taslaklar", "utkast", "kladde",
)

_LIST_LINE = re.compile(rb"^\((?P<flags>[^)]*)\)\s+(?P<delim>\"[^\"]*\"|NIL)\s+(?P<name>.*)$")


class ImapLike(Protocol):
    """Only the five calls this module makes. Narrow on purpose: it is what
    makes the decision logic testable without a socket."""
    def capability(self) -> tuple[str, list[bytes]]: ...
    def list(self, directory: str = '""', pattern: str = "*") -> tuple[str, list[Any]]: ...
    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[Any]]: ...
    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]: ...
    def append(self, mailbox: str, flags: str, date_time: Any, message: bytes) -> tuple[str, list[Any]]: ...


def _decode(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw
    try:
        # IMAP names are modified UTF-7; Python's codec is registered as utf-7
        # only for the standard variant, so decode leniently and fall back.
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")


def parse_list_line(line: bytes | str) -> tuple[list[str], str]:
    """`(\\HasNoChildren \\Sent) "/" "Sent Items"` -> `(["\\Sent"], "Sent Items")`."""
    raw = line if isinstance(line, bytes) else line.encode("utf-8", "replace")
    match = _LIST_LINE.match(raw.strip())
    if not match:
        return [], ""
    flags = _decode(match.group("flags")).split()
    name = _decode(match.group("name")).strip()
    if name.startswith('"') and name.endswith('"') and len(name) >= 2:
        name = name[1:-1]
    return flags, name


def discover(imap: ImapLike, attribute: str, names: tuple[str, ...],
             override: str = "", *, label: str = "mailbox") -> str:
    """A special-use mailbox, by attribute first and by name second.

    Flags before names is not a preference. A server that advertises the
    attribute has told us the answer; a name match is a guess that gets it
    wrong for every account whose owner renamed the folder or runs a localised
    client. Mailspring resolves it in exactly this order, and Thunderbird still
    carries Google's pre-standard `XLIST` alongside RFC 6154 -- so the old
    spelling is checked too rather than assumed dead.
    """
    if override.strip():
        return override.strip()
    try:
        typ, lines = imap.list()
    except Exception as exc:                      # noqa: BLE001
        raise LookupError(f"could not list mailboxes: {exc}") from exc
    if typ != "OK" or not lines:
        raise LookupError("the server returned no mailboxes")

    parsed = [parse_list_line(line) for line in lines if line]
    wanted = attribute.lower()
    # One pass, case-folded: RFC 6154 and Google's XLIST spell the attribute
    # identically, and servers differ on capitalisation.
    for flags, name in parsed:
        if name and any(f.lower() == wanted for f in flags):
            return name
    for flags, name in parsed:
        if name and name.split("/")[-1].strip().lower() in names:
            return name
    raise LookupError(
        f"no {label} mailbox found. Pick one in Settings → Sending so your "
        f"messages are filed where you expect."
    )


def discover_sent(imap: ImapLike, override: str = "") -> str:
    return discover(imap, SENT_ATTRIBUTE, SENT_NAMES, override, label="Sent")


def discover_drafts(imap: ImapLike, override: str = "") -> str:
    return discover(imap, DRAFTS_ATTRIBUTE, DRAFTS_NAMES, override, label="Drafts")


def find_by_message_id(imap: ImapLike, mailbox: str, message_id: str) -> list[bytes]:
    """UIDs in `mailbox` whose Message-ID matches. Empty list, never an error,
    when the mailbox cannot be opened -- a failed *search* must not stop us
    filing the copy."""
    try:
        typ, _ = imap.select(mailbox)
        if typ != "OK":
            return []
        typ, data = imap.uid("SEARCH", None, "HEADER", "Message-ID", f'"{message_id}"')
    except Exception:                              # noqa: BLE001
        return []
    if typ != "OK" or not data or not data[0]:
        return []
    raw = data[0] if isinstance(data[0], bytes) else str(data[0]).encode()
    return raw.split()


# How long to wait for the server to file its own copy before we file ours.
# Mailspring calls this "waiting for the sent folder to settle": the SMTP
# submission and the IMAP write are two different systems, and the second one
# lags. Appending too early is how a mailbox ends up with two of everything.
SETTLE_DELAYS = (0.0, 0.5, 1.5, 3.0)


def save_copy(
    imap: ImapLike,
    raw: bytes,
    message_id: str,
    *,
    override: str = "",
    delays: tuple[float, ...] = SETTLE_DELAYS,
    sleep=time.sleep,
) -> tuple[str, str]:
    """Find the server's own copy, or upload one. Returns `(outcome, detail)`.

    **Never deletes anything.** Mailspring removes the duplicates it finds;
    this does not, and reports them instead. Deleting a message from a user's
    Sent folder on the strength of a header match is a much worse failure than
    showing them two, and it is not undoable.
    """
    mailbox = discover_sent(imap, override)
    for index, delay in enumerate(delays):
        if delay:
            sleep(delay)
        found = find_by_message_id(imap, mailbox, message_id)
        if len(found) > 1:
            return "server", f"{mailbox} (the server filed {len(found)} copies)"
        if found:
            return "server", mailbox
        if index == 0 and not delays[1:]:
            break
    typ, data = imap.append(mailbox, "(\\Seen)", None, raw)
    if typ != "OK":
        raise LookupError(f"could not append to {mailbox}: {data}")
    return "appended", mailbox
