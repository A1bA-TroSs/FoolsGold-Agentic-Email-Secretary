"""Mail transport contract.

The mirror image of `sources/base.py`. A source's job is to put normalised rows
into the database; a transport's job is to take a finished RFC 5322 message and
get it off the machine. Everything above this line -- the compose pane, the
reply builder, the drafting model -- is written once and does not know which
one is installed.

Synchronous on purpose. `smtplib` and `imaplib` are blocking, FastAPI already
runs sync endpoints in a threadpool, and a send is a single user-initiated act
rather than a background loop. Making it `async` would buy an `await` and an
extra way to get the error handling wrong.

**Nothing in this package may be called without the user having seen and
approved the exact message.** That gate lives in the router, where the user
is; the rule is written here because this is the file someone reads before
wiring up a new caller.
"""
from __future__ import annotations

import email.message
import email.utils
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# How a message leaves. The distinction is not cosmetic: it decides what the
# button says, whether the user has anything left to do, and whether "sent" is
# a true thing to tell them.
#
#   DELIVERS   -- we hand the message to a server that will deliver it. SMTP,
#                 Graph, the Gmail API. After this, it is gone.
#   HANDS_OFF  -- we put the finished message somewhere the user's own mail
#                 client will find it, and they send it. Nothing has been
#                 delivered; calling it "sent" would be a lie.
#
# The second mode exists because on some accounts it is the *only* mode. A
# university tenant can refuse app registration, disable SMTP submission, and
# still sync IMAP perfectly well -- and a message appended to Drafts with its
# threading headers intact is a complete, correct reply waiting for one click.
DELIVERS = "delivers"
HANDS_OFF = "hands_off"


class TransportError(RuntimeError):
    """Something the user can act on. The message is shown verbatim, so it is
    written for them and not for a log file."""


@dataclass
class TransportStatus:
    ready: bool
    detail: str = ""
    account: str | None = None
    needs_setup: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "detail": self.detail,
            "account": self.account,
            "needs_setup": self.needs_setup,
            **self.extra,
        }


# What happened to the copy in Sent. Worth its own vocabulary because every
# provider does something different and the failure modes are user-visible:
# duplicate sent items, or none at all.
SENT_SERVER = "server"       # the server filed it; we did nothing
SENT_APPENDED = "appended"   # we uploaded a copy
SENT_SKIPPED = "skipped"     # configured off
SENT_FAILED = "failed"       # the mail went out; the copy did not


@dataclass
class SendResult:
    message_id: str
    sent_at: str
    # False means the message exists somewhere the user can find it and has
    # NOT been delivered. Nothing in the UI may say "sent" when this is False.
    delivered: bool = True
    # Where a handed-off message is waiting, in words the user can act on:
    # "Drafts", "[Gmail]/임시보관함", "a new Mail window".
    handoff: str = ""
    sent_copy: str = SENT_SKIPPED
    detail: str = ""
    recipients: tuple[str, ...] = ()
    # Addresses the server took the message for and then refused. `sendmail`
    # raises only when *every* recipient is refused; a partial refusal is a
    # return value, so without this field the message goes to four of five
    # people and the app reports unqualified success.
    refused: tuple[str, ...] = ()


def envelope_recipients(message: email.message.Message) -> list[str]:
    """Who the server is told to deliver to -- To, Cc **and Bcc**.

    Bcc is the whole reason this is a function. The addresses must be in the
    envelope or the blind recipients get nothing; the header must not be in the
    transmitted message or they are not blind. Two requirements that pull in
    opposite directions, from one field.
    """
    out: list[str] = []
    seen: set[str] = set()
    for header in ("To", "Cc", "Bcc"):
        for _, address in email.utils.getaddresses(message.get_all(header, [])):
            address = (address or "").strip()
            if "@" not in address:
                continue
            key = address.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(address)
    return out


def transmissible(message: email.message.EmailMessage) -> bytes:
    """The bytes that go on the wire: the message with `Bcc` removed.

    Done here rather than leaning on `smtplib.send_message`, which also strips
    it. Not because the stdlib is wrong, but because a privacy guarantee that
    lives in someone else's code is one nobody on this side can test, and this
    one is a single line to get wrong and impossible to notice afterwards.
    """
    import copy
    carbon = copy.copy(message)
    del carbon["Bcc"]
    del carbon["Resent-Bcc"]
    return carbon.as_bytes()


def sender_address(message: email.message.Message) -> str:
    for header in ("Sender", "From"):
        for _, address in email.utils.getaddresses(message.get_all(header, [])):
            if "@" in (address or ""):
                return address.strip()
    return ""


def check_sendable(message: email.message.EmailMessage) -> None:
    """Refuse a half-built message before it reaches a server.

    Every one of these has been shipped by somebody: a draft with no
    recipients, a message with no `Message-ID` (unthreadable and a spam
    signal), one with no `Date`. The compose layer sets all three; this is the
    assertion that nothing downstream quietly stopped doing so.
    """
    if not sender_address(message):
        raise TransportError("This message has no From address.")
    if not envelope_recipients(message):
        raise TransportError("This message has no recipients.")
    if not message.get("Message-ID"):
        raise TransportError("This message has no Message-ID.")
    if not message.get("Date"):
        raise TransportError("This message has no Date.")


class MailTransport(ABC):
    name: str = "base"
    label: str = "Mail"
    #: DELIVERS or HANDS_OFF -- see the constants above.
    mode: str = DELIVERS

    @abstractmethod
    def status(self) -> TransportStatus:
        """Cheap, synchronous, never raises. Called on every page load."""

    @abstractmethod
    def send(self, message: email.message.EmailMessage) -> SendResult:
        """Get the message out of this app. Raises `TransportError` with
        something the user can act on.

        A `DELIVERS` transport transmits it and deals with the copy in Sent. A
        `HANDS_OFF` transport puts it where the user's own client will find it
        and returns `delivered=False`.
        """


class NullTransport(MailTransport):
    """No transport configured. Exists so the rest of the app never has to
    branch on `None`, and so Settings has something to describe."""
    name = "none"
    label = "Not configured"

    def status(self) -> TransportStatus:
        return TransportStatus(
            ready=False,
            detail="Fools Gold can read your mail but cannot send any yet. "
                   "Add an outgoing server in Settings.",
            needs_setup=True,
        )

    def send(self, message: email.message.EmailMessage) -> SendResult:
        raise TransportError(
            "No outgoing mail server is configured, so nothing was sent. "
            "Add one in Settings → Sending."
        )


def check_bcc_survives_handoff(message: email.message.EmailMessage) -> None:
    """A handed-off message keeps its `Bcc` header, and that is correct.

    The opposite of the delivery path, and worth stating because the two rules
    look contradictory. On delivery the envelope carries the blind recipients
    and the header must be stripped, or they are not blind. On handoff there is
    no envelope yet -- the user's client builds one when they press send -- so
    stripping the header would silently drop those recipients instead.
    """
    if "Bcc" in message and not message.get_all("Bcc"):
        raise TransportError("This message has an empty Bcc header.")
