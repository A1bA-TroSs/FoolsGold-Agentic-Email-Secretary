"""Hand the finished message to the user's own mail client, via IMAP Drafts.

**Why this exists, measured rather than assumed.** The obvious macOS answer was
to hand a reply to Apple Mail. Two things were tested on a real machine and both
came back negative:

  * Mail.app's `outgoing message` class exposes `sender`, `subject`, `content`,
    `visible`, `message signature` and `id` -- and nothing for `In-Reply-To`,
    `References` or headers of any kind;
  * a `.eml` file opens in a **read-only** window, not as an editable draft.

So neither AppleScript nor a file handoff can carry threading headers. Anything
built on them starts a new conversation every time the user replies.

IMAP does not have that problem. `APPEND` takes a complete RFC 5322 message and
stores it verbatim, headers and all, so a reply written here arrives in Drafts
already threaded, already addressed, needing one click in whatever client the
user already uses.

**And it is the mode some accounts have and no other.** A university tenant can
refuse app registration (no Graph), disable SMTP submission (no direct send),
and still sync IMAP perfectly well. Nothing here is specific to one provider or
one machine: RFC 6154 for discovery, RFC 5322 for the message.

The trade this makes honestly: nothing is delivered. `SendResult.delivered` is
False and the UI must not say "sent".
"""
from __future__ import annotations

import email.message
import imaplib
import socket
import ssl
import time
from datetime import datetime, timezone

from .. import db
from .base import (
    HANDS_OFF, MailTransport, SendResult, TransportError, TransportStatus,
    check_sendable, envelope_recipients,
)
from .imap_folders import discover_drafts

TIMEOUT = 30.0


def _port(raw: str, fallback: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return fallback


def _settings() -> dict[str, str]:
    return {
        # IMAP can be configured on its own: this transport needs no SMTP at
        # all, and on the accounts it exists for there may not be any.
        "host": (db.get_setting("imap_host", "") or db.get_setting("smtp_host", "")).strip(),
        "port": db.get_setting("imap_port", "993").strip(),
        "username": (db.get_setting("imap_username", "")
                     or db.get_setting("smtp_username", "")).strip(),
        "password": db.get_setting("smtp_password", ""),
        "folder": db.get_setting("drafts_folder", "").strip(),
    }


class ImapDraftTransport(MailTransport):
    name = "imap_draft"
    label = "Save to Drafts"
    mode = HANDS_OFF

    def status(self) -> TransportStatus:
        cfg = _settings()
        if not cfg["host"]:
            return TransportStatus(
                ready=False, needs_setup=True,
                detail="No IMAP server set. This is the incoming server your "
                       "mail client already uses.",
            )
        if not cfg["username"] or not cfg["password"]:
            return TransportStatus(
                ready=False, needs_setup=True, account=cfg["username"] or None,
                detail="This server needs a username and password. Many providers "
                       "want an app-specific password here, not your normal one.",
            )
        return TransportStatus(
            ready=True,
            account=cfg["username"],
            detail=f"{cfg['host']}:{_port(cfg['port'], 993)} — replies are written "
                   f"to Drafts for you to send from your own mail client.",
            extra={"mode": HANDS_OFF, "drafts_folder": cfg["folder"] or None},
        )

    def _imap(self, cfg: dict[str, str]) -> imaplib.IMAP4_SSL:
        client = imaplib.IMAP4_SSL(cfg["host"], _port(cfg["port"], 993), timeout=TIMEOUT)
        client.login(cfg["username"], cfg["password"])
        return client

    def send(self, message: email.message.EmailMessage) -> SendResult:
        check_sendable(message)
        state = self.status()
        if not state.ready:
            raise TransportError(state.detail)
        cfg = _settings()

        # The whole message, Bcc header included. There is no envelope here --
        # the user's client builds one when they press send -- so stripping Bcc
        # would drop those recipients rather than hide them, which is the exact
        # opposite of what it does on the delivery path.
        raw = message.as_bytes()

        client = None
        try:
            client = self._imap(cfg)
            folder = discover_drafts(client, cfg["folder"])
            typ, data = client.append(
                folder, "(\\Draft)", imaplib.Time2Internaldate(time.time()), raw)
            if typ != "OK":
                raise TransportError(f"The server refused the draft: {data}")
        except TransportError:
            raise
        except LookupError as exc:
            raise TransportError(str(exc)) from exc
        except (imaplib.IMAP4.error, OSError, socket.timeout, ssl.SSLError) as exc:
            raise TransportError(
                f"Could not save the draft to {cfg['host']} — {exc}") from exc
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:                  # noqa: BLE001
                    pass

        return SendResult(
            message_id=str(message.get("Message-ID") or ""),
            sent_at=datetime.now(timezone.utc).isoformat(),
            delivered=False,
            handoff=folder,
            detail=f"Saved to {folder}. Open it in your mail client and press "
                   f"send — the reply is already addressed and threaded.",
            recipients=tuple(envelope_recipients(message)),
        )
