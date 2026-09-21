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
from ..transports.base import HANDS_OFF, TransportError
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


def _me() -> Mailbox:
    address = db.get_setting("user_address", "").strip()
    if "@" not in address:
        raise HTTPException(
            status_code=400,
            detail="Set your own email address in Settings first — it goes in "
                   "the From line of anything you send.")
    return Mailbox(address=address, name=db.get_setting("user_name", "").strip())


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
    me = _me()
    to, cc, bcc = _boxes(body.to), _boxes(body.cc), _boxes(body.bcc)

    if body.action == "new":
        if not to:
            raise HTTPException(status_code=400, detail="Add at least one recipient.")
        message = build_new(sender=me, to=to, cc=cc, bcc=bcc,
                            subject=body.subject, text=body.text)
    elif body.action == "forward":
        if not to:
            raise HTTPException(status_code=400, detail="Add at least one recipient.")
        message = build_forward(_parent(body.email_id), sender=me, to=to, cc=cc, bcc=bcc,
                                text=body.text, as_attachment=body.as_attachment)
    else:
        parent = _parent(body.email_id)
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
