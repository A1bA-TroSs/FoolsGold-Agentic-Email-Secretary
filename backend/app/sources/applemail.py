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
import os
import plistlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .. import db
from .base import MailSource, SourceError, SourceStatus

MAIL_HOME = Path.home() / "Library" / "Mail"

FULL_DISK_ACCESS_HINT = (
    "macOS is blocking access to ~/Library/Mail. This is Full Disk Access, not a "
    "missing mailbox. Open System Settings → Privacy & Security → Full Disk Access "
    "and add the app you launched from — Terminal if you ran `npm start` there — "
    "then QUIT AND REOPEN it. macOS never applies the change to a running process."
)

# Mailboxes we never want in a priority inbox.
EXCLUDED_MAILBOXES = {
    "trash", "deleted messages", "deleted items", "junk", "junk e-mail", "spam",
    "sent", "sent messages", "sent items", "drafts", "outbox", "archive",
    "notes", "rss",
}

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
    if any(name.strip().lower() in EXCLUDED_MAILBOXES for name in chain):
        return False
    if inbox_only:
        return chain[0].strip().lower() == "inbox"
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
        "folder": "/".join(chain),
        "body_preview": (text or _HTML_TAG.sub(" ", html))[:200].replace("\n", " ").strip(),
        "body_text": text,
        "body_html": html,
        "synced_at": db.now_iso(),
    }


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

SENT_MAILBOXES = {"sent", "sent messages", "sent items"}
_AFFINITY_CACHE: dict[str, tuple[float, dict[str, int]]] = {}
_AFFINITY_TTL_SECONDS = 6 * 3600
_AFFINITY_LOOKBACK_DAYS = 365
_AFFINITY_MAX_FILES = 3000


def _iter_sent_files(root: Path) -> Iterator[tuple[Path, float]]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if d != "Attachments"]
        if os.path.basename(dirpath) != "Messages":
            continue
        chain = _mailbox_chain(Path(dirpath), root)
        if not chain or chain[0].strip().lower() not in SENT_MAILBOXES:
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
    if cached and not force and (now - cached[0]) < _AFFINITY_TTL_SECONDS:
        return cached[1]

    cutoff = now - _AFFINITY_LOOKBACK_DAYS * 86400
    counts: dict[str, int] = {}
    try:
        files = [f for f in _iter_sent_files(root) if f[1] >= cutoff]
    except (PermissionError, OSError):
        files = []
    files.sort(key=lambda f: f[1], reverse=True)

    parser = BytesHeaderParser(policy=email.policy.default)
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
            for _name, address in email.utils.getaddresses([str(v) for v in values]):
                if address:
                    counts[address.lower()] = counts.get(address.lower(), 0) + 1
        except Exception:  # noqa: BLE001
            continue

    _AFFINITY_CACHE[key] = (now, counts)
    return counts


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

    def status(self) -> SourceStatus:
        """Never raises. The base class promises this and the frontend relies on
        it -- /api/health calls it on every page load, so an exception here takes
        down the whole app rather than showing the one instruction that fixes it.
        """
        try:
            return self._status()
        except PermissionError:
            return SourceStatus(ready=False, needs_setup=True, detail=FULL_DISK_ACCESS_HINT)
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
                )
            if not MAIL_HOME.exists():
                return SourceStatus(
                    ready=False,
                    needs_setup=True,
                    detail=(
                        "No ~/Library/Mail directory found. Add your account to Apple Mail "
                        "and let it finish downloading, then hit refresh."
                    ),
                )
            return SourceStatus(
                ready=False,
                needs_setup=True,
                detail="Found ~/Library/Mail but no V<n> message store inside it.",
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
