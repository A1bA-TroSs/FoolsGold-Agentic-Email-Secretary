"""Picks the transport the user configured in Settings."""
from __future__ import annotations

from .. import db
from .base import MailTransport, NullTransport
from .drafts_transport import ImapDraftTransport
from .smtp_transport import SmtpTransport

TRANSPORTS: dict[str, type[MailTransport]] = {
    "none": NullTransport,
    "smtp": SmtpTransport,
    "imap_draft": ImapDraftTransport,
}


def get_transport(name: str | None = None) -> MailTransport:
    """Defaults to `none`, not to a guess.

    `sources/registry.py` falls back to Apple Mail when the setting is
    unrecognised, because reading the wrong mailbox shows you something you can
    ignore. Sending through a transport the user did not choose is not
    recoverable, so an unknown name here means *nothing is sent*.
    """
    name = (name or db.get_setting("mail_transport", "none") or "none").lower()
    return TRANSPORTS.get(name, NullTransport)()


def all_status() -> dict[str, dict]:
    return {name: {**cls().status().as_dict(), "mode": cls.mode, "label": cls.label}
            for name, cls in TRANSPORTS.items()}
