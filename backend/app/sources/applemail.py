"""Apple Mail local store reader -- no authentication of any kind.

Why this exists: Microsoft requires an Entra app registration to reach a
mailbox over Graph, and many university tenants block their students from
creating one. Meanwhile Apple Mail has *already* done the OAuth dance and has
the mail sitting on disk. So we read that instead. No app registration, no
API quota, no network, works offline, and completely outside the reach of a
school IT policy.

Storage layout (stable since 2005):

    ~/Library/Mail/V<n>/<account-uuid>/<Mailbox>.mbox/.../Messages/*.emlx

Each .emlx file is three parts:

    1. a line holding the byte count of part 2
    2. the RFC822 message itself
    3. an Apple plist with Mail's own metadata (read flag, attachment count...)

We parse the files directly rather than reading Mail's `Envelope Index`
SQLite database. That database's schema changes between macOS releases and it
is locked while Mail is running; the .emlx plist carries the read flag we
actually need, so the fragile dependency buys us nothing.

macOS protects ~/Library/Mail behind TCC: the process doing the reading needs
**Full Disk Access**. See docs/APPLE_MAIL_SETUP.md.
"""
from __future__ import annotations

import email
import email.policy
import email.utils
from email.parser import BytesHeaderParser
import hashlib
import json
import unicodedata
from urllib.parse import unquote
import os
import plistlib
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .. import db
from .base import MailSource, SourceError, SourceStatus

MAIL_HOME = Path.home() / "Library" / "Mail"

FULL_DISK_ACCESS_HINT = (
    "macOS has not given FoolsGold access to your mail yet. Turn FoolsGold on in "
    "System Settings > Privacy & Security > Full Disk Access, then restart FoolsGold."
)


def _launcher() -> str:
    """Who macOS holds responsible for reading ~/Library/Mail -- which is who
    needs Full Disk Access. A packaged build runs this backend frozen, as a child
    of FoolsGold.app, so the answer is the app. A checkout runs it with Python
    from the terminal that ran `npm start`, so the answer is that terminal, and
    only a developer ever sees that case.

    The UI words both; this only names which applies. The old hint told someone
    who had downloaded the app to "add the app you launched from -- Terminal if
    you ran `npm start`", which is a sentence written to a developer."""
    return "app" if getattr(sys, "frozen", False) else "terminal"


def _fda_status() -> "SourceStatus":
    return SourceStatus(ready=False, needs_setup=True, detail=FULL_DISK_ACCESS_HINT,
                        extra={"detail_key": "fdaNeeded",
                               "detail_vars": {"launcher": _launcher()}})

# Mailboxes we never want in a priority inbox, by kind. Apple Mail names the
# standard mailboxes in the system language, and the folder name on disk is
# that localised name (Danny's Mac, 2026-11-03: 받은 편지함, 정크 메일, 보낸 편지함).
# An English-only list let 141 junk mails and 82 of his own sent mails into the
# inbox views and hid his Sent folder from the correspondent walk.
_MAILBOX_KINDS: dict[str, set[str]] = {
    "junk": {"junk", "junk e-mail", "junk email", "spam", "bulk mail",
             "정크 메일", "정크메일", "스팸", "스팸 메일", "迷惑メール", "迷惑メッセージ", "スパム",
             "垃圾邮件", "垃圾郵件", "垃圾信件"},
    "sent": {"sent", "sent messages", "sent items", "sent mail",
             "보낸 편지함", "보낸편지함", "보낸 메일", "送信済み", "送信済みメール",
             "送信済みメッセージ", "送信済みアイテム", "已发送", "已发送邮件", "已傳送",
             "已傳送郵件", "寄件備份"},
    "drafts": {"drafts", "draft", "임시 보관함", "임시보관함", "下書き", "草稿", "草稿箱"},
    "trash": {"trash", "deleted messages", "deleted items", "bin", "휴지통", "지운 편지함",
              "ゴミ箱", "削除済み", "削除済みアイテム", "废纸篓", "已删除", "已删除邮件",
              "垃圾桶", "已刪除", "已刪除郵件"},
    "archive": {"archive", "보관", "보관함", "아카이브", "アーカイブ", "归档", "歸檔", "封存"},
    "outbox": {"outbox", "보낼 편지함", "送信トレイ", "送信箱", "发件箱", "寄件匣"},
    "other": {"notes", "rss"},
}
# One list of localised Sent and Drafts names for the whole app: the IMAP
# folder finder's lists are merged in rather than kept as a second copy.
from ..transports.imap_folders import DRAFTS_NAMES as _IMAP_DRAFTS, SENT_NAMES as _IMAP_SENT  # noqa: E402
_MAILBOX_KINDS["sent"] |= set(_IMAP_SENT)
_MAILBOX_KINDS["drafts"] |= set(_IMAP_DRAFTS)
_KIND_OF = {unicodedata.normalize("NFC", name).casefold(): kind
            for kind, names in _MAILBOX_KINDS.items() for name in names}
_INBOX_NAMES = {"inbox", "받은 편지함", "받은편지함", "受信", "受信トレイ", "受信箱",
                "收件箱", "收件匣"}


def _norm_name(name: str) -> str:
    # macOS file names are often NFD; a Korean name read from disk does not
    # equal the same name typed in source until both are NFC.
    return " ".join(unicodedata.normalize("NFC", name or "").casefold().split())


def mailbox_kind(name: str) -> str | None:
    """"junk", "sent", "drafts", "trash", "archive", "outbox" (or "other" for
    Notes and RSS) for a standard mailbox in any of the languages above;
    None for the inbox and the user's own folders."""
    return _KIND_OF.get(_norm_name(name))


def excluded_kinds(chain: list[str]) -> set[str]:
    """The excluded kinds a mailbox path falls under. Junk, Sent, Drafts,
    Trash and Outbox anywhere in the path (Gmail nests them under [Gmail]);
    Archive only as the account's top mailbox, so a user's own "Projects/보관"
    folder is kept."""
    kinds = set()
    for depth, name in enumerate(chain):
        kind = mailbox_kind(name)
        # Archive, and the short English "bin", only as the account's top
        # mailbox: a user's "Projects/보관" or "Recycling/Bin" folder is theirs.
        top_only = kind == "archive" or _norm_name(name) == "bin"
        if kind and (not top_only or depth == 0):
            kinds.add(kind)
    return kinds


def is_inbox(name: str) -> bool:
    return _norm_name(name) in _INBOX_NAMES


# Kept for callers that test a single name; every kind is excluded.
EXCLUDED_MAILBOXES = set(_KIND_OF)

# Apple Mail's flags bitfield, as stored in the .emlx plist.
FLAG_READ = 1 << 0
FLAG_ANSWERED = 1 << 2
FLAG_FLAGGED = 1 << 4
FLAG_DRAFT = 1 << 6

_MAX_BODY_CHARS = 20000
_HTML_TAG = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------
# locating the store
# --------------------------------------------------------------------------

def find_mail_root(explicit: str | None = None) -> Path | None:
    """Newest V<n> directory under ~/Library/Mail, or an override from Settings.

    The version bumps with most macOS releases (V9, V10, V11...), and old ones
    are left behind, so 'highest number wins' is the only stable rule.

    Raises PermissionError when macOS (TCC) blocks the directory. That is a
    *useful* distinction -- "grant Full Disk Access" is a different instruction
    from "no mail found" -- so it deliberately propagates rather than collapsing
    to None. Every caller is responsible for turning it into an actionable
    message; see AppleMailSource.status().
    """
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_dir() else None
    if not MAIL_HOME.is_dir():
        return None

    versioned: list[tuple[int, Path]] = []
    for child in MAIL_HOME.iterdir():
        if child.is_dir() and child.name.startswith("V") and child.name[1:].isdigit():
            versioned.append((int(child.name[1:]), child))
    if versioned:
        return max(versioned)[1]
    # Very old layouts kept mailboxes directly under ~/Library/Mail.
    return MAIL_HOME if any(MAIL_HOME.glob("*.mbox")) else None


def _mailbox_chain(path: Path, root: Path) -> list[str]:
    """The .mbox names between the account directory and this message."""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    return [p[:-5] for p in parts if p.endswith(".mbox")]


def _wanted_mailbox(chain: list[str], inbox_only: bool) -> bool:
    if not chain:
        return False
    if excluded_kinds(chain):
        return False
    if inbox_only:
        return is_inbox(chain[0])
    return True


def iter_message_files(root: Path, inbox_only: bool = True) -> Iterator[tuple[Path, float, list[str]]]:
    """Walk for .emlx files, yielding (path, mtime, mailbox chain).

    os.walk with a pruned directory list, because a long-lived mailbox can hold
    tens of thousands of files and we do not want to stat the attachment
    directories that sit beside them.

    Note on error handling: os.walk *silently swallows* directory errors by
    default. Without the onerror hook below, a mail store that macOS is blocking
    for lack of Full Disk Access would look exactly like an empty one -- and the
    user would be told "no mail downloaded yet" when the real answer is "grant a
    permission". That is the single most likely failure on a fresh install, so it
    has to name itself. We only raise when the walk produced nothing at all: one
    unreadable folder deep inside a working store should be skipped, not fatal.
    """
    errors: list[OSError] = []
    produced = False

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=errors.append):
        dirnames[:] = [d for d in dirnames if d != "Attachments"]
        if os.path.basename(dirpath) != "Messages":
            continue
        here = Path(dirpath)
        chain = _mailbox_chain(here, root)
        if not _wanted_mailbox(chain, inbox_only):
            continue
        for name in filenames:
            if not name.endswith(".emlx"):
                continue
            full = here / name
            try:
                stat = full.stat()
            except OSError as exc:
                errors.append(exc)
                continue
            produced = True
            yield full, stat.st_mtime, chain

    if not produced:
        for exc in errors:
            if isinstance(exc, PermissionError):
                raise exc


def has_inbox(root: Path) -> bool:
    for _ in iter_message_files(root, inbox_only=True):
        return True
    return False


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

_HEADER_BYTES = 16384   # RFC822 headers come first; this is plenty for Date/From


def read_header_date(path: Path, fallback_mtime: float) -> float:
    """Read just the headers to get the real received time.

    This exists because file mtime is close to useless for freshness: when Apple
    Mail first syncs an account it writes every message to disk at once, so a
    five-year-old email and this morning's both have today's mtime. Sorting by
    mtime then produces an essentially arbitrary "newest 300".

    mtime is still a valid *lower* bound -- a file cannot be written before the
    message arrived -- so it stays as a cheap pre-filter. This is the authority.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(_HEADER_BYTES)
    except OSError:
        return fallback_mtime

    newline = head.find(b"\n")
    if newline != -1 and head[:newline].strip().isdigit():
        head = head[newline + 1 :]           # drop the .emlx byte-count line

    try:
        headers = BytesHeaderParser(policy=email.policy.default).parsebytes(head)
        raw = headers.get("Date")
        if raw:
            when = email.utils.parsedate_to_datetime(str(raw))
            if when is not None:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return when.timestamp()
    except Exception:  # noqa: BLE001 - a malformed header must not stop the scan
        pass
    return fallback_mtime


@dataclass
class ParsedEmlx:
    message: email.message.Message
    plist: dict[str, Any]

    @property
    def flags(self) -> int:
        raw = self.plist.get("flags")
        if isinstance(raw, int):
            return raw
        if isinstance(raw, dict):  # some tools rewrite this as a dict
            value = 0
            value |= FLAG_READ if raw.get("read") else 0
            value |= FLAG_ANSWERED if raw.get("answered") else 0
            value |= FLAG_FLAGGED if raw.get("flagged") else 0
            return value
        return 0

    @property
    def is_read(self) -> bool:
        return bool(self.flags & FLAG_READ)

    @property
    def is_answered(self) -> bool:
        """The strongest 'I dealt with this' signal available without asking."""
        return bool(self.flags & FLAG_ANSWERED)

    @property
    def is_flagged(self) -> bool:
        """The strongest 'I care about this' signal available without asking."""
        return bool(self.flags & FLAG_FLAGGED)

    @property
    def attachment_count(self) -> int:
        return (self.flags >> 10) & 0b111111


def parse_emlx(path: Path) -> ParsedEmlx:
    """Split the three sections. Written defensively: a single unreadable
    message must never abort a whole sync."""
    raw = path.read_bytes()
    newline = raw.find(b"\n")
    if newline == -1:
        raise SourceError(f"{path.name} is not an .emlx file")

    try:
        length = int(raw[:newline].strip())
    except ValueError:
        # No byte count -- treat the whole file as a plain .eml.
        length, newline = len(raw), -1

    start = newline + 1
    body = raw[start : start + length]
    trailer = raw[start + length :]

    message = email.message_from_bytes(body, policy=email.policy.default)

    plist: dict[str, Any] = {}
    marker = trailer.find(b"<?xml")
    if marker != -1:
        try:
            loaded = plistlib.loads(trailer[marker:])
            if isinstance(loaded, dict):
                plist = loaded
        except Exception:  # noqa: BLE001 - metadata is optional, the message is not
            pass
    return ParsedEmlx(message=message, plist=plist)


def _addresses(message: email.message.Message, header: str) -> list[dict[str, str]]:
    values = message.get_all(header, [])
    out = []
    for name, address in email.utils.getaddresses([str(v) for v in values]):
        if address:
            out.append({"name": name or "", "address": address.lower()})
    return out


def _decode(part: email.message.Message) -> str:
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        return ""
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def extract_bodies(message: email.message.Message) -> tuple[str, str]:
    """Returns (plain_text, html). Prefers a real text/plain part and only
    falls back to stripping the HTML when the sender did not provide one."""
    text_parts: list[str] = []
    html_parts: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = str(part.get("Content-Disposition") or "")
        if "attachment" in disposition.lower():
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            text_parts.append(_decode(part))
        elif content_type == "text/html":
            html_parts.append(_decode(part))

    html = "\n".join(html_parts)[:_MAX_BODY_CHARS]
    text = "\n".join(text_parts).strip()
    if not text and html:
        from ..graph.sync import html_to_text  # same stripper both sources use
        text = html_to_text(html)
    return text[:_MAX_BODY_CHARS], html


def _received_at(message: email.message.Message, parsed: ParsedEmlx, fallback_mtime: float) -> str:
    raw = message.get("Date")
    if raw:
        try:
            when = email.utils.parsedate_to_datetime(str(raw))
            if when is not None:
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                return when.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            pass
    received = parsed.plist.get("date-received")
    if isinstance(received, (int, float)):
        return datetime.fromtimestamp(received, tz=timezone.utc).isoformat()
    return datetime.fromtimestamp(fallback_mtime, tz=timezone.utc).isoformat()


def _stable_id(message: email.message.Message, path: Path) -> str:
    """Message-ID where the sender supplied one, otherwise a hash of the path.

    This has to stay identical across syncs or the same email gets re-imported
    and re-classified, which on a metered provider costs real money.
    """
    message_id = message.get("Message-ID") or message.get("Message-Id")
    if message_id:
        cleaned = str(message_id).strip().strip("<>")
        if cleaned:
            return "am:" + hashlib.sha1(cleaned.encode("utf-8", "replace")).hexdigest()
    return "am:path:" + hashlib.sha1(str(path).encode("utf-8", "replace")).hexdigest()


def to_row(path: Path, mtime: float, chain: list[str]) -> dict[str, Any]:
    parsed = parse_emlx(path)
    message = parsed.message
    sender = _addresses(message, "From")
    text, html = extract_bodies(message)
    subject = str(message.get("Subject") or "(no subject)")

    return {
        "id": _stable_id(message, path),
        "conversation_id": str(message.get("Thread-Index") or message.get("References") or "")[:120] or None,
        "subject": subject,
        "from_name": sender[0]["name"] if sender else "",
        "from_address": sender[0]["address"] if sender else "",
        "to_recipients": json.dumps(_addresses(message, "To")),
        "cc_recipients": json.dumps(_addresses(message, "Cc")),
        "received_at": _received_at(message, parsed, mtime),
        "is_read": 1 if parsed.is_read else 0,
        "is_answered": 1 if parsed.is_answered else 0,
        "is_flagged": 1 if parsed.is_flagged else 0,
        "has_attachments": 1 if parsed.attachment_count else 0,
        "importance": _importance(message),
        "web_link": "",                       # nothing to link to; it is a local file
        # macOS gives directory names in NFD: "보낸 편지함" arrives as decomposed
        # jamo, which is a different string from the NFC "보낸 편지함" every
        # constant in this codebase is written in. Sent-folder detection
        # (compose/identity.py) matched nothing on a Korean mailbox because of
        # it -- normalise once, here, where the filesystem is the source.
        "folder": unicodedata.normalize("NFC", "/".join(chain)),
        "body_preview": (text or _HTML_TAG.sub(" ", html))[:200].replace("\n", " ").strip(),
        "body_text": text,
        "body_html": html,
        # Kept so inline images can be served from the original file later.
        "source_path": str(path),
        "synced_at": db.now_iso(),
    }


# Inline images cap out here. A mail body is allowed to reference a picture;
# it is not allowed to make the app read an arbitrary 200MB file into memory
# because a header said it was a logo.
MAX_INLINE_BYTES = 8 * 1024 * 1024
INLINE_TYPES = ("image/",)


def _normalise_cid(raw: str) -> str:
    """RFC 2392: a `cid:` URL is the Content-ID with the angle brackets
    removed and the value percent-encoded.

    Both halves of that sentence are places to get this wrong, and both were
    wrong here. `msg.get('Content-ID')` returns `<logo@example.com>` WITH the
    brackets, while the HTML says `src="cid:logo@example.com"` without them --
    so a naive equality check never matches and every inline image in the
    mailbox renders as a broken icon. And characters illegal in a URL arrive
    percent-encoded, so `cid:a%25b@h` is the header value `<a%b@h>`.
    """
    value = unquote((raw or "").strip())
    if value.startswith("<") and value.endswith(">"):
        value = value[1:-1]
    return value.strip().lower()


# Magic numbers, so a sidecar file is only served when it really is an image.
# The declared Content-Type comes from the message; the bytes come from a
# directory looked up by index. If the two disagree, the index was wrong.
_IMAGE_MAGIC = (
    b"\x89PNG\r\n\x1a\n",            # png
    b"\xff\xd8\xff",                   # jpeg
    b"GIF87a", b"GIF89a",               # gif
    b"BM",                              # bmp
    b"II*\x00", b"MM\x00*",             # tiff
    b"<svg", b"<?xml",                  # svg
)


def _looks_like_image(blob: bytes) -> bool:
    if not blob:
        return False
    if blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
        return True
    if blob[4:12] in (b"ftypavif", b"ftypheic", b"ftypheix", b"ftypmif1"):
        return True
    return blob.startswith(_IMAGE_MAGIC)


def _numbered_parts(
    message: email.message.Message, prefix: tuple[int, ...] = ()
) -> Iterator[tuple[email.message.Message, tuple[int, ...]]]:
    """Every leaf part, tagged with the number Apple Mail files it under.

    Apple names each attachment directory after a dotted path of 1-based
    child indices from the top-level message -- `2`, `1.3`, and so on. Two
    details are easy to get wrong and both change the answer:

    * *every* child consumes an index, including the text/plain and text/html
      bodies, so an image is rarely part 1;
    * a `message/rfc822` wrapper consumes an index but the message inside it
      does not, so parts of a forwarded mail continue the wrapper's number
      rather than nesting one level deeper.

    Verified against mailsplit's `partNr`, which is what
    qqilihq/partial-emlx-converter builds these paths from.
    """
    if message.get_content_type() == "message/rfc822":
        payload = message.get_payload()
        if isinstance(payload, list) and payload:
            yield from _numbered_parts(payload[0], prefix)
        return
    if message.get_content_maintype() == "multipart":
        payload = message.get_payload()
        if isinstance(payload, list):
            for index, child in enumerate(payload, start=1):
                yield from _numbered_parts(child, prefix + (index,))
        return
    yield message, prefix


def _attachment_dir(path: Path, part_nr: tuple[int, ...]) -> Path | None:
    """`.../Messages/123.partial.emlx` -> `.../Attachments/123/1.3`."""
    if not part_nr:
        return None
    stem = path.name.split(".", 1)[0]     # 123.partial.emlx -> 123, not 123.partial
    if not stem:
        return None
    return path.parent.parent / "Attachments" / stem / ".".join(str(n) for n in part_nr)


def _sidecar_bytes(
    path: Path, part_nr: tuple[int, ...], part: email.message.Message
) -> bytes | None:
    """The real bytes for a part Apple left out of the .emlx file.

    The filename inside the directory is not fixed: Mail uses a
    *locale-specific* default ("Mail-Anhang.jpeg" on a German system) when the
    part declares no filename. So try the declared name first and fall back to
    reading the directory, which normally holds exactly one file.
    """
    directory = _attachment_dir(path, part_nr)
    if directory is None:
        return None
    names: list[str] = []
    declared = part.get_filename()
    if declared:
        names.append(declared)
    try:
        names.extend(
            entry.name for entry in directory.iterdir()
            if entry.is_file() and not entry.name.startswith(".")
        )
    except OSError:
        pass
    try:
        root = directory.resolve()
    except OSError:
        return None
    seen: set[str] = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            candidate = (directory / name).resolve()
            if root not in candidate.parents:
                continue              # a declared filename is attacker-controlled
            if not candidate.is_file() or candidate.stat().st_size > MAX_INLINE_BYTES:
                continue
            blob = candidate.read_bytes()
        except OSError:
            continue
        if _looks_like_image(blob):
            return blob
    return None


def inline_parts(path: Path) -> dict[str, tuple[str, bytes]]:
    """Every inline image in this message, keyed by normalised Content-ID.

    Read on demand from the original file rather than stored: see the
    `source_path` migration for why.

    **The bytes are often not in the .emlx file.** A message whose attachments
    Apple Mail left on the server is written as `.partial.emlx`: part headers
    intact, payload replaced by a placeholder plus `X-Apple-Content-Length`.
    Content-ID matches, payload is empty, image is broken -- and it looks
    exactly like a lookup bug, which is how it has been reported more than
    once. The bytes do exist, in a sibling `Attachments/<id>/<part-nr>/`
    directory, so they are reassembled here rather than given up on.
    """
    out: dict[str, tuple[str, bytes]] = {}
    try:
        message = parse_emlx(path).message
    except (OSError, SourceError):
        return out
    for part, part_nr in _numbered_parts(message):
        cid = part.get("Content-ID") or part.get("Content-Id")
        if not cid:
            continue
        ctype = (part.get_content_type() or "").lower()
        if not ctype.startswith(INLINE_TYPES):
            continue
        detached = bool(part.get("X-Apple-Content-Length"))
        payload = _sidecar_bytes(path, part_nr, part) if detached else None
        if payload is None:
            try:
                blob = part.get_payload(decode=True)
            except Exception:             # noqa: BLE001 - one bad part is not fatal
                blob = None
            # A detached part's inline payload is a placeholder, not an image,
            # so it has to pass the magic check before it is trusted.
            if blob and (not detached or _looks_like_image(blob)):
                payload = blob
        if not payload or len(payload) > MAX_INLINE_BYTES:
            continue
        out[_normalise_cid(cid)] = (ctype, payload)
    return out


def _importance(message: email.message.Message) -> str:
    raw = str(message.get("Importance") or "").strip().lower()
    if raw in ("high", "low", "normal"):
        return raw
    priority = str(message.get("X-Priority") or "").strip()[:1]
    if priority in ("1", "2"):
        return "high"
    if priority in ("4", "5"):
        return "low"
    return "normal"


# --------------------------------------------------------------------------
# the source
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# correspondent affinity
# --------------------------------------------------------------------------

SENT_MAILBOXES = _MAILBOX_KINDS["sent"]
_AFFINITY_CACHE: dict[str, tuple[float, dict[str, int], tuple[int, float]]] = {}
_AFFINITY_TTL_SECONDS = 6 * 3600
_AFFINITY_LOOKBACK_DAYS = 365
_AFFINITY_MAX_FILES = 3000


def _iter_sent_files(root: Path) -> Iterator[tuple[Path, float]]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if d != "Attachments"]
        if os.path.basename(dirpath) != "Messages":
            continue
        chain = _mailbox_chain(Path(dirpath), root)
        if not chain or not any(mailbox_kind(name) == "sent" for name in chain):
            continue
        for name in filenames:
            if name.endswith(".emlx"):
                full = Path(dirpath) / name
                try:
                    yield full, full.stat().st_mtime
                except OSError:
                    continue


def build_correspondent_affinity(root: Path, force: bool = False) -> dict[str, int]:
    """Who the user actually writes to, and how often.

    This is the closest thing to a ground-truth "I care about this person"
    signal that can be read without asking or guessing: replying to someone is a
    deliberate act. Inferring the same thing from received mail alone is
    hopeless -- a mailing list sends far more than a supervisor does.

    Headers only, capped and cached, because this walks a second mailbox.
    """
    key = str(root)
    cached = _AFFINITY_CACHE.get(key)
    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - _AFFINITY_LOOKBACK_DAYS * 86400
    # The stat walk is cheap; the header reads are not. Re-read only when the
    # Sent folder changed, so a reply sent a minute ago reaches the sent log
    # (and leaves To respond) on the next sync instead of hours later.
    try:
        files = [f for f in _iter_sent_files(root) if f[1] >= cutoff]
    except (PermissionError, OSError):
        files = []
    stamp = (len(files), max((m for _, m in files), default=0.0))
    if cached and not force and cached[2] == stamp and (now - cached[0]) < _AFFINITY_TTL_SECONDS:
        return cached[1]
    counts: dict[str, int] = {}
    files.sort(key=lambda f: f[1], reverse=True)

    parser = BytesHeaderParser(policy=email.policy.default)
    sent: list[dict[str, Any]] = []
    for path, _mtime in files[:_AFFINITY_MAX_FILES]:
        try:
            with open(path, "rb") as handle:
                head = handle.read(_HEADER_BYTES)
        except OSError:
            continue
        newline = head.find(b"\n")
        if newline != -1 and head[:newline].strip().isdigit():
            head = head[newline + 1 :]
        try:
            headers = parser.parsebytes(head)
            values = headers.get_all("To", []) + headers.get_all("Cc", [])
            recipients = []
            for _name, address in email.utils.getaddresses([str(v) for v in values]):
                if address:
                    counts[address.lower()] = counts.get(address.lower(), 0) + 1
                    recipients.append(address.lower())
            sent.append({"key": str(path), "subject": str(headers.get("Subject") or ""),
                         "sent_at": _header_iso(headers.get("Date"), _mtime),
                         "recipients": recipients,
                         "from_name": email.utils.parseaddr(str(headers.get("From") or ""))[0],
                         "from_address": email.utils.parseaddr(str(headers.get("From") or ""))[1]})
        except Exception:  # noqa: BLE001
            continue

    # The same walk feeds the sent log (A1: a reply anywhere in a thread
    # answers it). Only a hashed subject key, the time and the recipients are
    # kept -- see db.record_sent.
    try:
        db.record_sent(sent)
    except Exception:  # noqa: BLE001 - the log is a hint; affinity must not fail over it
        pass
    _AFFINITY_CACHE[key] = (now, counts, stamp)
    return counts


def _header_iso(value: Any, fallback: float) -> str:
    try:
        dt = email.utils.parsedate_to_datetime(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return datetime.fromtimestamp(fallback, timezone.utc).isoformat()


class AppleMailSource(MailSource):
    name = "applemail"
    label = "Apple Mail on this Mac"

    def correspondents(self) -> dict[str, int]:
        """Addresses the user has written to, mapped to how many times."""
        try:
            root = self._root()
        except (PermissionError, OSError):
            return {}
        return build_correspondent_affinity(root) if root else {}

    def _root(self) -> Path | None:
        return find_mail_root(db.get_setting("applemail_root", "").strip() or None)

    # ------------------------------------------------------- back to the file

    def _source_file(self, email_id: str) -> Path | None:
        """The `.emlx` a row came from, or None.

        **The containment check lives here and not in the router.** A stored
        path is data. The first time anything other than this reader writes to
        that column, an endpoint that takes an id from a URL and opens the file
        it names becomes a path traversal -- so the check belongs beside the
        only code that knows what a legitimate path looks like, where it cannot
        be forgotten by a second caller.
        """
        with db.connect() as conn:
            row = conn.execute(
                "SELECT source_path FROM emails WHERE id = ?", (email_id,)).fetchone()
        if row is None or not row["source_path"]:
            return None
        path = Path(row["source_path"])
        try:
            root = self._root()
            if root is None or not path.resolve().is_relative_to(Path(root).resolve()):
                return None
            # Regular files only. A named pipe inside the mail root would make
            # the read below block forever, and this is reachable by id.
            return path if path.is_file() else None
        except (OSError, PermissionError):
            return None

    def inline_part(self, email_id: str, cid: str) -> tuple[str, bytes] | None:
        path = self._source_file(email_id)
        if path is None:
            return None
        return inline_parts(path).get(_normalise_cid(cid))

    def parent_message(self, email_id: str):
        """Every threading header, read back off disk.

        No migration needed, and none wanted: `_stable_id` hashes `Message-ID`
        into the row id on purpose, so it survives a re-sync. The file still has
        the original, so the answer is to go and read it rather than to store a
        second copy that can drift.
        """
        from ..compose import ParentMessage
        path = self._source_file(email_id)
        if path is None:
            return None
        try:
            parsed = parse_emlx(path)
        except (OSError, SourceError):
            return None
        text, html = extract_bodies(parsed.message)
        return ParentMessage.from_headers(
            parsed.message, body_text=text, body_html=html, raw=parsed.message.as_bytes())

    def status(self) -> SourceStatus:
        """Never raises. The base class promises this and the frontend relies on
        it -- /api/health calls it on every page load, so an exception here takes
        down the whole app rather than showing the one instruction that fixes it.
        """
        try:
            return self._status()
        except PermissionError:
            return _fda_status()
        except OSError as exc:
            return SourceStatus(ready=False, needs_setup=True, detail=str(exc))

    def _status(self) -> SourceStatus:
        # Resolve the root first: an explicit path from Settings must win, so a
        # store that lives somewhere other than ~/Library/Mail still works.
        root = self._root()
        if root is None:
            override = db.get_setting("applemail_root", "").strip()
            if override:
                return SourceStatus(
                    ready=False,
                    needs_setup=True,
                    detail=f"The mail store path in Settings does not exist: {override}",
                    extra={"detail_key": "mailRootMissing", "detail_vars": {"path": override}},
                )
            if not MAIL_HOME.exists():
                return SourceStatus(
                    ready=False,
                    needs_setup=True,
                    detail=(
                        "No ~/Library/Mail directory found. Add your account to Apple Mail "
                        "and let it finish downloading, then hit refresh."
                    ),
                    extra={"detail_key": "mailNotSetUp"},
                )
            return SourceStatus(
                ready=False,
                needs_setup=True,
                detail="Found ~/Library/Mail but no V<n> message store inside it.",
                extra={"detail_key": "mailStoreEmpty"},
            )
        # PermissionError and OSError here are handled by status() above.
        next(iter(iter_message_files(root, inbox_only=False)), None)

        account = db.get_setting("user_address", "").strip()
        return SourceStatus(
            ready=True,
            account=account or "Apple Mail (local)",
            detail=f"Reading {root}",
            needs_setup=not account,
            extra={"mail_root": str(root)},
        )

    async def sync(self, days: int | None = None, max_messages: int | None = None) -> dict[str, Any]:
        try:
            root = self._root()
        except PermissionError as exc:
            raise SourceError(FULL_DISK_ACCESS_HINT) from exc
        if root is None:
            raise SourceError(
                "Could not find Apple Mail's message store. Add an account to Apple Mail, "
                "or set the path manually in Settings."
            )

        db.purge_excluded_folders()
        days = days if days is not None else int(db.get_setting("sync_days", "30") or 30)
        max_messages = max_messages if max_messages is not None else int(
            db.get_setting("sync_max_messages", "300") or 300
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()

        inbox_only = (db.get_setting("applemail_inbox_only", "true") or "true").lower() == "true"
        try:
            candidates = list(iter_message_files(root, inbox_only=inbox_only))
            # If this account labels its inbox as something else, do not show an
            # empty app -- fall back to every non-excluded mailbox.
            if not candidates and inbox_only:
                candidates = list(iter_message_files(root, inbox_only=False))
        except PermissionError as exc:
            raise SourceError(FULL_DISK_ACCESS_HINT) from exc

        # Nothing on disk has changed since the last look -- so do not read
        # fourteen thousand files again to find that out.
        #
        # The poller runs every five minutes, forever. On a Mac where Mail.app
        # is not running it had scanned 14,135 files **two hundred times in
        # twenty-four hours**, nine seconds a go, to discover nothing each
        # time. That is the number the "frozen" banner was counting. The walk
        # above is cheap -- one stat per file -- and the expensive part is the
        # header read on every candidate below, which is pure waste when the
        # store is untouched.
        #
        # The fingerprint is the file count and the newest mtime. A new message
        # is a new file, so it moves both; a deletion moves the count. It lives
        # in settings rather than in memory because a five-minute poller in a
        # desktop app is restarted often enough for an in-memory guard to miss
        # most of what it is for.
        fingerprint = f"{len(candidates)}:{max((m for _, m, _ in candidates), default=0.0):.0f}"
        if db.get_setting("applemail_scan_fingerprint", "") == fingerprint and db.email_count():
            # Reported as a real sync, not as nothing: the freshness banner is
            # driven by what the mailbox contains, and a scan that correctly
            # found no new mail is exactly as informative as one that parsed
            # three hundred files to reach the same answer.
            return {
                "source": self.name, "root": str(root), "scanned": len(candidates),
                "in_window": 0, "fetched": 0, "written": 0, "skipped": 0,
                "unchanged": True,
                "correspondents": len(build_correspondent_affinity(root)),
            }

        # Two passes. mtime alone cannot be trusted for freshness (a first sync
        # stamps every file with today), but it is a valid lower bound, so use it
        # to cheaply drop what definitely predates the window. Then read each
        # remaining file's headers for the real Date, and only fully parse the
        # newest max_messages of those.
        coarse = [c for c in candidates if c[1] >= cutoff]
        dated = [(path, read_header_date(path, mtime), mtime, chain)
                 for path, mtime, chain in coarse]
        in_window = [d for d in dated if d[1] >= cutoff]
        in_window.sort(key=lambda d: d[1], reverse=True)
        selected = in_window[:max_messages]

        rows: list[dict[str, Any]] = []
        skipped = 0
        for path, _received, mtime, chain in selected:
            try:
                rows.append(to_row(path, mtime, chain))
            except Exception:  # noqa: BLE001 - one bad message must not stop the sync
                skipped += 1

        written = db.upsert_emails(rows)

        # Stamped only after a full pass has written what it found. Stamping
        # before the parse would make a sync that died halfway look complete,
        # and the next two hundred polls would skip the work it missed.
        db.set_setting("applemail_scan_fingerprint", fingerprint)

        return {
            "source": self.name,
            "root": str(root),
            "scanned": len(candidates),
            "in_window": len(in_window),
            "fetched": len(rows),
            "written": written,
            "skipped": skipped,
            "correspondents": len(build_correspondent_affinity(root)),
        }
