"""Writing mail, and the gate in front of sending it.

**The gate is the shape of the API, not a check inside it.** `POST /draft`
builds a message and stores exactly the bytes that would be transmitted;
`POST /{token}/send` accepts a token and *nothing else*. There is no field on
the send request that can change a recipient, a subject or a word of the body,
so the only thing that can leave this machine is a message the server already
rendered and handed back for the user to read.

That is a stronger promise than "the handler validates its input", and it is
the one worth making here: every reviewed design for agent email-sending puts a
human in front of the send, and the ones that hold are the ones where the
approval is bound to the *content* rather than to a flag.
"""
from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db
from ..compose import Mailbox, build_forward, build_new, build_reply
from ..sources.registry import get_source
from ..compose.identity import known_identities, pick_sender
from ..transports.base import HANDS_OFF, TransportError
from ..transports import accounts
from ..transports.auto_transport import NeedsPassword, verify_password
from ..transports.registry import get_transport

router = APIRouter(prefix="/api/compose", tags=["compose"])

ACTIONS = ("new", "reply", "reply_all", "forward")


class DraftIn(BaseModel):
    action: str = "new"
    email_id: str | None = None
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    subject: str = ""
    text: str = ""
    quote: bool = True
    as_attachment: bool = False


def _boxes(raw: list[str]) -> list[Mailbox]:
    """Accepts what a person types: `a@b.c`, `Name <a@b.c>`, several per line,
    separated by commas or semicolons."""
    out: list[Mailbox] = []
    seen: set[str] = set()
    for line in raw:
        for box in Mailbox.parse((line or "").replace(";", ",")):
            if box.key in seen:
                continue
            seen.add(box.key)
            out.append(box)
    return out


def _me(parent=None) -> Mailbox:
    """The address this message goes out from.

    For a reply or forward, the address the original was *delivered to* --
    "answer from the account I am reading", which is what people mean and
    almost never what a single default address gives them. For a new message,
    the configured address. See compose/identity.py for the order.
    """
    chosen = pick_sender(parent, known_identities(),
                         name=db.get_setting("user_name", "").strip())
    if chosen is None:
        raise HTTPException(
            status_code=400,
            detail="Set your own email address in Settings first — it goes in "
                   "the From line of anything you send.")
    return chosen


def _parent(email_id: str | None):
    if not email_id:
        raise HTTPException(status_code=400, detail="This action needs a message to act on.")
    parent = get_source().parent_message(email_id)
    if parent is None:
        # Not a crash, and not silent: a source with no way back to the
        # original cannot produce threading headers, and a reply that quietly
        # starts a new conversation is worse than one that refuses.
        raise HTTPException(
            status_code=409,
            detail="The original message could not be read back, so a reply "
                   "would not stay in its conversation.")
    return parent


def _summary(message, action: str, email_id: str | None) -> dict[str, Any]:
    def header(name: str) -> str:
        return str(message.get(name) or "")
    body = message.get_body(("plain",))
    return {
        "action": action,
        "email_id": email_id,
        "from": header("From"),
        "to": header("To"),
        "cc": header("Cc"),
        "bcc": header("Bcc"),
        "subject": header("Subject"),
        "message_id": header("Message-ID"),
        "in_reply_to": header("In-Reply-To"),
        "references": header("References"),
        "text": body.get_content() if body else "",
    }


def _transport_view() -> dict[str, Any]:
    transport = get_transport()
    state = transport.status()
    return {
        "name": transport.name, "label": transport.label, "mode": transport.mode,
        "ready": state.ready, "detail": state.detail,
        # The UI needs this to label the button. "Send" on a transport that only
        # files a draft is a lie the user finds out about later.
        "verb": "saveDraft" if transport.mode == HANDS_OFF else "send",
    }


@router.post("/draft")
def draft(body: DraftIn) -> dict:
    if body.action not in ACTIONS:
        raise HTTPException(status_code=400, detail=f"action must be one of {list(ACTIONS)}")
    to, cc, bcc = _boxes(body.to), _boxes(body.cc), _boxes(body.bcc)
    parent = _parent(body.email_id) if body.action != "new" else None
    me = _me(parent)

    if body.action == "new":
        if not to:
            raise HTTPException(status_code=400, detail="Add at least one recipient.")
        message = build_new(sender=me, to=to, cc=cc, bcc=bcc,
                            subject=body.subject, text=body.text)
    elif body.action == "forward":
        if not to:
            raise HTTPException(status_code=400, detail="Add at least one recipient.")
        message = build_forward(parent, sender=me, to=to, cc=cc, bcc=bcc,
                                text=body.text, as_attachment=body.as_attachment)
    else:
        message = build_reply(
            parent, sender=me, text=body.text, quote=body.quote,
            reply_all=(body.action == "reply_all"),
            # Empty means "use the addresses the reply rules derived"; a
            # non-empty list is the user having edited the field, and their
            # choice outranks ours.
            to=to or None, cc=cc or None, bcc=bcc)

    token = secrets.token_urlsafe(18)
    summary = _summary(message, body.action, body.email_id)
    db.save_draft(token, action=body.action, email_id=body.email_id,
                  mime=message.as_bytes(), summary=summary)
    return {"token": token, "summary": summary, "transport": _transport_view()}


class CredentialsIn(BaseModel):
    address: str
    password: str


@router.post("/credentials")
def credentials(body: CredentialsIn) -> dict:
    """Remember the password for one sending address -- after proving it.

    Logs in to the account's own server first and saves nothing unless that
    works, so a typo is caught here rather than at the moment of sending.
    Sends nothing. Accepted only for an address the user is known to send
    as: this is not a way to store credentials for arbitrary accounts.
    """
    address = (body.address or "").strip().lower()
    if address not in known_identities():
        raise HTTPException(status_code=400, detail="That is not one of your addresses.")
    if not body.password:
        raise HTTPException(status_code=400, detail="Enter the password.")
    try:
        ok, why = verify_password(address, body.password)
    except TransportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        return {"ok": False, "address": address, "reason": why.split(":")[0], "detail": why}
    accounts.remember_password(address, body.password)
    return {"ok": True, "address": address}


@router.delete("/credentials/{address}")
def forget_credentials(address: str) -> dict:
    """Forget a remembered password. The next reply from that address asks again."""
    accounts.forget_password(address.strip().lower())
    return {"ok": True, "accounts": accounts.summary()}


@router.get("/{token}")
def show(token: str) -> dict:
    stored = db.get_draft(token)
    if stored is None:
        raise HTTPException(status_code=404, detail="That draft has expired.")
    return {"token": token, "summary": stored["summary"], "sent_at": stored["sent_at"],
            "outcome": stored["outcome"], "transport": _transport_view()}


@router.post("/{token}/send")
def send(token: str) -> dict:
    """Takes a token. Deliberately takes nothing else — see the module docstring."""
    stored = db.get_draft(token)
    if stored is None:
        raise HTTPException(status_code=404, detail="That draft has expired. Write it again.")
    if stored["sent_at"]:
        # Idempotent rather than an error: a double click, or a retry after the
        # reply was lost, must not put a second copy in someone's inbox.
        return {"token": token, "already": True, "sent_at": stored["sent_at"],
                "outcome": stored["outcome"], "summary": stored["summary"]}

    import email.policy
    from email import message_from_bytes
    message = message_from_bytes(bytes(stored["mime"]), policy=email.policy.SMTP)

    try:
        # `raw` is the approved bytes. The parsed message is only for the
        # transport to read headers from; re-serialising it would re-fold them.
        result = get_transport().send(message, raw=bytes(stored["mime"]))
    except NeedsPassword as exc:
        # Not a failure: the one question automatic sending ever asks, answered
        # in the compose window. The draft stays unsent and sendable.
        raise HTTPException(status_code=409, detail={
            "needs_password": exc.address, "reason": exc.reason,
            "message": str(exc)}) from exc
    except TransportError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    outcome = {
        "delivered": result.delivered, "handoff": result.handoff,
        "sent_copy": result.sent_copy, "detail": result.detail,
        "recipients": list(result.recipients), "refused": list(result.refused),
        "message_id": result.message_id, "sent_at": result.sent_at,
    }
    db.mark_draft_sent(token, outcome, result.sent_at)
    return {"token": token, "already": False, "sent_at": result.sent_at,
            "outcome": outcome, "summary": stored["summary"]}
