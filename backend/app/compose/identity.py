"""Which of the user's addresses a reply should come from.

"Reply from the account I am reading" is what a person means, and it is almost
always *the address the message was delivered to*. A department mails a list;
the list delivers to you; your reply should come from you at that address, not
from whichever account happens to be the default.

The order is Thunderbird's catch-all rule (mail.compose.catchAllHeaders):
Delivered-To, Envelope-To, X-Original-To, then To, then Cc -- the delivery
headers first, because they name *you* where To may only name a list.
"""
from __future__ import annotations

import unicodedata

from .. import db
from .message import Mailbox, ParentMessage


def known_identities() -> list[str]:
    """Every address the user is known to send as, most certain first.

    The configured address, then every address that appears as the sender of
    mail in a Sent folder -- which is the one list that is true by
    construction, whatever the provider and whatever language its folders are
    named in.
    """
    from ..transports.imap_folders import SENT_NAMES
    out: list[str] = []
    configured = (db.get_setting("user_address", "") or "").strip().lower()
    if "@" in configured:
        out.append(configured)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT folder, lower(from_address) AS a, COUNT(*) AS n FROM emails "
            "WHERE from_address LIKE '%@%' GROUP BY folder, a ORDER BY n DESC").fetchall()
    for row in rows:
        # NFC as well as lower(): a folder name that came off a macOS
        # filesystem is decomposed, and decomposed Hangul is not equal to the
        # composed constants in SENT_NAMES. Rows written before this fix are
        # still in the database, so normalising here is not redundant.
        leaf = unicodedata.normalize(
            "NFC", (row["folder"] or "").split("/")[-1]).strip().lower()
        if leaf in SENT_NAMES and row["a"] not in out:
            out.append(row["a"])
    return out


def pick_sender(parent: ParentMessage | None, identities: list[str] | None = None,
                name: str = "") -> Mailbox | None:
    """The identity to reply as, or None when the user has no known address."""
    mine = [a.lower() for a in (identities if identities is not None else known_identities())]
    if not mine:
        return None
    if parent is not None:
        for group in (parent.delivered_to, parent.to, parent.cc):
            for box in group:
                if box.key in mine:
                    return Mailbox(box.address, name if box.key == mine[0] else "")
    return Mailbox(mine[0], name)
