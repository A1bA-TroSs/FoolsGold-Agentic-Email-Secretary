"""SMTP submission, with an IMAP copy to Sent.

Chosen as the first transport for one reason: **nothing gates it.** Graph needs
an Entra app a university tenant may refuse to let a student register; the
Gmail API needs an OAuth app in a verification tier. SMTP with an app password
needs a host, a port and a password the user already has, so it proves the
transport interface against a real server before any of that starts.

What it is *not* is a universal answer. Google's app passwords need 2-Step
Verification and are unavailable to Workspace, security-key-only and Advanced
Protection accounts; Microsoft's basic auth for SMTP submission is scheduled to
stop working in Exchange Online at the end of 2026, and many tenants have
already disabled it per-mailbox. Both of those are reasons the transport
*interface* exists, not reasons to skip this one.
"""
from __future__ import annotations

import email.message
import email.utils
import imaplib
import smtplib
import socket
import ssl
from datetime import datetime, timezone

from .. import db
from .base import (
    SENT_APPENDED, SENT_FAILED, SENT_SERVER, SENT_SKIPPED,
    MailTransport, SendResult, TransportError, TransportStatus,
    check_sendable, envelope_recipients, sender_address, transmissible,
)
from .imap_folders import open_imap, save_copy

TIMEOUT = 30.0

SECURITY_STARTTLS = "starttls"
SECURITY_SSL = "ssl"
SECURITY_PLAIN = "plain"

COPY_AUTO = "auto"
COPY_SKIP = "skip"


def _settings() -> dict[str, str]:
    return {
        "host": db.get_setting("smtp_host", "").strip(),
        "port": db.get_setting("smtp_port", "587").strip(),
        "security": db.get_setting("smtp_security", SECURITY_STARTTLS).strip().lower(),
        "username": db.get_setting("smtp_username", "").strip(),
        "password": db.get_setting("smtp_password", ""),
        "imap_host": db.get_setting("imap_host", "").strip(),
        "imap_port": db.get_setting("imap_port", "993").strip(),
        "sent_copy": db.get_setting("sent_copy", COPY_AUTO).strip().lower(),
        "sent_folder": db.get_setting("sent_folder", "").strip(),
        "imap_security": db.get_setting("imap_security", "ssl").strip().lower(),
    }


def _port(raw: str, fallback: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return fallback


class SmtpTransport(MailTransport):
    name = "smtp"
    label = "SMTP"

    # ------------------------------------------------------------ status

    def status(self) -> TransportStatus:
        cfg = _settings()
        if not cfg["host"]:
            return TransportStatus(
                ready=False, needs_setup=True,
                detail="No outgoing server set. Your provider calls this SMTP.",
            )
        if not cfg["username"] or not cfg["password"]:
            return TransportStatus(
                ready=False, needs_setup=True, account=cfg["username"] or None,
                detail="This server needs a username and password. Many providers "
                       "want an app-specific password here, not your normal one.",
            )
        copies = cfg["sent_copy"] != COPY_SKIP
        return TransportStatus(
            ready=True,
            account=cfg["username"],
            detail=f"{cfg['host']}:{_port(cfg['port'], 587)} ({cfg['security']})",
            extra={"saves_to_sent": copies, "sent_folder": cfg["sent_folder"] or None},
        )

    # ------------------------------------------------------------ sending

    def send(self, message: email.message.EmailMessage,
             raw: bytes | None = None) -> SendResult:
        check_sendable(message)
        cfg = _settings()
        state = self.status()
        if not state.ready:
            raise TransportError(state.detail)

        sender = sender_address(message)
        recipients = envelope_recipients(message)
        raw = transmissible(message, raw)

        refused = self._transmit(cfg, sender, recipients, raw)

        # Past this line the mail is *gone*. Nothing below may raise, because
        # a failure to file the copy is not a failure to send, and reporting it
        # as one invites the user to send again.
        outcome, detail = self._save_copy(cfg, raw, str(message.get("Message-ID") or ""))
        if refused:
            detail = (f"Delivered, except to {', '.join(sorted(refused))}, "
                      f"which the server refused. " + detail).strip()
        return SendResult(
            message_id=str(message.get("Message-ID") or ""),
            sent_at=datetime.now(timezone.utc).isoformat(),
            sent_copy=outcome,
            detail=detail,
            recipients=tuple(a for a in recipients if a not in refused),
            refused=tuple(sorted(refused)),
        )

    def _transmit(
        self, cfg: dict[str, str], sender: str, recipients: list[str], raw: bytes
    ) -> set[str]:
        """Returns the addresses the server accepted the message for and then
        refused -- **not** an error.

        `sendmail` raises `SMTPRecipientsRefused` only when *every* recipient is
        rejected. Refuse four of five and it returns them in a dict and the send
        is a success, so a caller that ignores the return value tells the user
        the mail went to everyone. Raising here would be just as wrong in the
        other direction: the other four already have it.
        """
        host = cfg["host"]
        security = cfg["security"]
        port = _port(cfg["port"], 465 if security == SECURITY_SSL else 587)
        context = ssl.create_default_context()
        try:
            if security == SECURITY_SSL:
                client = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=context)
            else:
                client = smtplib.SMTP(host, port, timeout=TIMEOUT)
            with client:
                client.ehlo()
                if security == SECURITY_STARTTLS:
                    # No fallback to plaintext. A server that cannot do
                    # STARTTLS on the submission port is a downgrade attack as
                    # often as it is a misconfiguration, and the password goes
                    # over that socket.
                    client.starttls(context=context)
                    client.ehlo()
                client.login(cfg["username"], cfg["password"])
                return set(client.sendmail(sender, recipients, raw))
        except smtplib.SMTPAuthenticationError as exc:
            raise TransportError(
                "The server rejected that username and password. If your "
                "provider uses two-factor authentication, this field usually "
                f"needs an app-specific password rather than your own. ({exc.smtp_code})"
            ) from exc
        except smtplib.SMTPNotSupportedError as exc:
            raise TransportError(
                f"{host} does not offer encrypted submission on port {port}. "
                "Nothing was sent, because the password would have gone out in "
                f"the clear. ({exc})"
            ) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            # Every recipient refused: nothing was transmitted at all.
            refused = ", ".join(exc.recipients)
            raise TransportError(
                f"Nothing was sent. The server refused every recipient: {refused}") from exc
        except smtplib.SMTPResponseException as exc:
            raise TransportError(f"{host} refused the message: {exc.smtp_code} {exc.smtp_error}") from exc
        except (OSError, socket.timeout, ssl.SSLError) as exc:
            raise TransportError(f"Could not reach {host}:{port} — {exc}") from exc

    # ------------------------------------------------------------ sent copy

    def _imap(self, cfg: dict[str, str]) -> imaplib.IMAP4:
        host = cfg["imap_host"] or cfg["host"]
        default = 143 if cfg["imap_security"] in ("starttls", "plain") else 993
        return open_imap(host, _port(cfg["imap_port"], default), cfg["imap_security"],
                         cfg["username"], cfg["password"], timeout=TIMEOUT)

    def check(self) -> dict:
        """Connect, negotiate, authenticate, and quit before `MAIL FROM`.
        Nothing is queued and nothing is sent."""
        state = self.status()
        if not state.ready:
            return {"ok": False, "detail": state.detail}
        cfg = _settings()
        security = cfg["security"]
        port = _port(cfg["port"], 465 if security == SECURITY_SSL else 587)
        context = ssl.create_default_context()
        try:
            client = (smtplib.SMTP_SSL(cfg["host"], port, timeout=TIMEOUT, context=context)
                      if security == SECURITY_SSL
                      else smtplib.SMTP(cfg["host"], port, timeout=TIMEOUT))
            with client:
                client.ehlo()
                if security == SECURITY_STARTTLS:
                    client.starttls(context=context)
                    client.ehlo()
                client.login(cfg["username"], cfg["password"])
            return {"ok": True, "detail": f"Signed in to {cfg['host']}:{port}."}
        except smtplib.SMTPAuthenticationError as exc:
            return {"ok": False, "detail": "The server rejected that username and password. "
                    f"Many providers want an app-specific password here. ({exc.smtp_code})"}
        except smtplib.SMTPNotSupportedError:
            return {"ok": False, "detail": f"{cfg['host']} does not offer encrypted "
                    f"submission on port {port}; the password was not sent."}
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            return {"ok": False, "detail": f"Could not reach {cfg['host']}:{port} — {exc}"}

    def _save_copy(self, cfg: dict[str, str], raw: bytes, message_id: str) -> tuple[str, str]:
        if cfg["sent_copy"] == COPY_SKIP:
            return SENT_SKIPPED, "turned off in Settings"
        client = None
        try:
            client = self._imap(cfg)
            outcome, detail = save_copy(
                client, raw, message_id, override=cfg["sent_folder"])
            return (SENT_SERVER if outcome == "server" else SENT_APPENDED), detail
        except Exception as exc:                   # noqa: BLE001 - see send()
            return SENT_FAILED, (
                f"Your message was sent. Saving a copy to Sent did not work: {exc}"
            )
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:                  # noqa: BLE001
                    pass
