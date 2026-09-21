"""Send from the address the user is replying as, with nothing to configure.

The user's model is "A -> B": I am reading A's mail, I answer B, it comes from
A. What IMAP and SMTP are, which port, which kind of encryption -- none of it
is theirs to know. So this transport is the default, and it works out the rest:

  1. the From address is already chosen (compose/identity.py);
  2. that address's servers are discovered once and remembered
     (autoconfig.py, accounts.py);
  3. the only question ever asked is the password, once, at the moment it is
     first needed -- raised as `NeedsPassword`, answered inline in the compose
     window, never in a settings form;
  4. delivery degrades by itself. Send directly if the account's server allows
     it; if it refuses submission -- which many university and company tenants
     do while still syncing mail normally -- put the finished message in the
     account's Drafts, threaded and addressed, and say so. The user presses
     send in their usual mail app. `delivered` stays honest either way.

A wrong password and a server that refuses all passwords are different
failures and are told apart: the first asks again, the second falls back.
"""
from __future__ import annotations

import email.message
import imaplib
import smtplib
import socket
import ssl
import time
from datetime import datetime, timezone

from . import accounts
from .autoconfig import ServerConfig, discover
from .base import (
    DELIVERS, SENT_APPENDED, SENT_FAILED, SENT_SERVER, MailTransport, SendResult,
    TransportError, TransportStatus, check_sendable, envelope_recipients,
    sender_address, transmissible,
)
from .imap_folders import discover_drafts, open_imap, quoted, save_copy

TIMEOUT = 25.0

# Microsoft 365's answer when a tenant has switched off password submission.
# The password may be perfectly correct; the door is simply closed.
_SUBMISSION_DISABLED = ("5.7.139", "smtpclientauthentication is disabled",
                        "5.7.57", "basic authentication is disabled")


class NeedsPassword(TransportError):
    """The one question this transport ever asks, raised where it is needed."""

    def __init__(self, address: str, reason: str = "missing"):
        self.address, self.reason = address, reason
        super().__init__(
            f"Enter the password for {address} to send from it."
            if reason == "missing" else
            f"{address} did not accept that password.")


class _SubmissionClosed(Exception):
    """SMTP will not take this message from this account; Drafts might."""


def servers_for(address: str) -> ServerConfig:
    found = accounts.config(address)
    if found is None:
        found = discover(address)
        if found is None:
            raise TransportError(f"{address} does not look like an email address.")
        accounts.remember_config(address, found)
    return found


def verify_password(address: str, secret: str) -> tuple[bool, str]:
    """Log in to the account's IMAP server. Used before a password is saved,
    so a typo is caught now rather than at the moment of sending."""
    cfg = servers_for(address)
    if cfg.oauth_only:
        return False, "oauth_only"
    client = None
    try:
        client = open_imap(cfg.imap_host, cfg.imap_port, cfg.imap_security,
                           cfg.imap_username, secret, timeout=TIMEOUT)
        return True, ""
    except imaplib.IMAP4.error:
        return False, "rejected"
    except (OSError, ssl.SSLError, LookupError) as exc:
        return False, f"unreachable: {exc}"
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:                                  # noqa: BLE001
                pass


class AutoTransport(MailTransport):
    name = "auto"
    label = "Automatic"
    mode = DELIVERS            # usually; a closed server makes it hand off instead

    def status(self) -> TransportStatus:
        return TransportStatus(
            ready=True,
            detail="Replies go out from the address each message was sent to. "
                   "Server settings are found automatically.",
            extra={"accounts": accounts.summary()},
        )

    def send(self, message: email.message.EmailMessage,
             raw: bytes | None = None) -> SendResult:
        check_sendable(message)
        sender = sender_address(message)
        cfg = servers_for(sender)
        if cfg.oauth_only:
            raise TransportError(
                f"{sender}'s provider only allows sign-in through its own login page, "
                "not with a password, and that is not supported yet. Nothing was sent.")
        secret = accounts.password(sender)
        if not secret:
            raise NeedsPassword(sender, "missing")

        approved = raw if raw is not None else message.as_bytes()
        recipients = envelope_recipients(message)
        try:
            refused = self._deliver(cfg, secret, sender, recipients,
                                    transmissible(message, approved))
        except _SubmissionClosed as why:
            return self._to_drafts(cfg, secret, sender, message, approved, str(why))

        accounts.remember_mode(sender, "delivered")
        # Past this line the mail is gone; nothing below may raise.
        outcome, where = self._file_sent_copy(cfg, secret, approved,
                                              str(message.get("Message-ID") or ""))
        detail = (f"Delivered, except to {', '.join(sorted(refused))}, which the "
                  f"server refused." if refused else "")
        return SendResult(
            message_id=str(message.get("Message-ID") or ""),
            sent_at=datetime.now(timezone.utc).isoformat(),
            sent_copy=outcome, detail=detail or where,
            recipients=tuple(a for a in recipients if a not in refused),
            refused=tuple(sorted(refused)))

    # ------------------------------------------------------------ delivery

    def _deliver(self, cfg: ServerConfig, secret: str, sender: str,
                 recipients: list[str], wire: bytes) -> set[str]:
        context = ssl.create_default_context()
        try:
            client = (smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=TIMEOUT,
                                       context=context)
                      if cfg.smtp_security == "ssl"
                      else smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=TIMEOUT))
            with client:
                client.ehlo()
                if cfg.smtp_security == "starttls":
                    client.starttls(context=context)      # never falls back to plaintext
                    client.ehlo()
                try:
                    client.login(cfg.smtp_username, secret)
                except smtplib.SMTPAuthenticationError as exc:
                    text = f"{exc.smtp_code} {exc.smtp_error!r}".lower()
                    if any(marker in text for marker in _SUBMISSION_DISABLED):
                        raise _SubmissionClosed("the server does not accept sending "
                                                "from mail apps for this account") from exc
                    # Wrong password, or a server that refuses SMTP but not IMAP.
                    # Only IMAP can tell those apart.
                    ok, _ = verify_password(sender, secret)
                    if not ok:
                        raise NeedsPassword(sender, "rejected") from exc
                    raise _SubmissionClosed("the server refused sending for this "
                                            "account, though the password is right") from exc
                return set(client.sendmail(sender, recipients, wire))
        except (NeedsPassword, _SubmissionClosed):
            raise
        except smtplib.SMTPNotSupportedError as exc:
            raise _SubmissionClosed("the server offers no encrypted sending") from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise TransportError("Nothing was sent. The server refused every recipient: "
                                 + ", ".join(exc.recipients)) from exc
        except smtplib.SMTPResponseException as exc:
            raise TransportError(f"The server refused the message: {exc.smtp_code} "
                                 f"{exc.smtp_error!r}") from exc
        except (OSError, socket.timeout, ssl.SSLError) as exc:
            raise _SubmissionClosed(f"the sending server could not be reached ({exc})") from exc

    def _to_drafts(self, cfg: ServerConfig, secret: str, sender: str,
                   message: email.message.EmailMessage, approved: bytes,
                   why: str) -> SendResult:
        client = None
        try:
            client = open_imap(cfg.imap_host, cfg.imap_port, cfg.imap_security,
                               cfg.imap_username, secret, timeout=TIMEOUT)
            folder = discover_drafts(client)
            # The approved bytes, Bcc included: there is no envelope yet, the
            # user's own client builds one when they press send.
            typ, data = client.append(quoted(folder), "(\\Draft)",
                                      imaplib.Time2Internaldate(time.time()), approved)
            if typ != "OK":
                raise TransportError(f"Could not save the draft: {data}")
        except imaplib.IMAP4.error as exc:
            raise NeedsPassword(sender, "rejected") from exc
        except LookupError as exc:
            raise TransportError(str(exc)) from exc
        except (OSError, ssl.SSLError) as exc:
            raise TransportError(
                f"Neither sending nor saving a draft worked for {sender}: {why}; "
                f"and {cfg.imap_host} could not be reached ({exc}). Nothing was sent.") from exc
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:                              # noqa: BLE001
                    pass
        accounts.remember_mode(sender, "drafts")
        return SendResult(
            message_id=str(message.get("Message-ID") or ""),
            sent_at=datetime.now(timezone.utc).isoformat(),
            delivered=False, handoff=folder,
            detail=f"Saved to {folder} because {why}.",
            recipients=tuple(envelope_recipients(message)))

    def _file_sent_copy(self, cfg, secret, raw, message_id) -> tuple[str, str]:
        client = None
        try:
            client = open_imap(cfg.imap_host, cfg.imap_port, cfg.imap_security,
                               cfg.imap_username, secret, timeout=TIMEOUT)
            outcome, where = save_copy(client, transmissible(None, raw), message_id)
            return (SENT_SERVER if outcome == "server" else SENT_APPENDED), where
        except Exception as exc:                               # noqa: BLE001
            return SENT_FAILED, f"Your message was sent. Saving a copy to Sent did not work: {exc}"
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:                              # noqa: BLE001
                    pass
