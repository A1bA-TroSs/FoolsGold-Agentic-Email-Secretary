"""Mail source contract.

A source's only job is to put normalised rows into the `emails` table. Where
they came from -- Microsoft Graph over the network, or Apple Mail's local store
on this Mac -- is invisible to classification, scoring, the digest and the UI.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:                       # avoids importing the compose package
    from ..compose import ParentMessage # at module load just for a type name


@dataclass
class SourceStatus:
    """What the UI needs to decide between 'show the dashboard', 'show the
    login screen' and 'show a setup hint'."""
    ready: bool
    detail: str = ""
    account: str | None = None
    needs_auth: bool = False       # a sign-in flow will fix this
    needs_setup: bool = False      # a settings value or an OS permission will fix it
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "detail": self.detail,
            "account": self.account,
            "needs_auth": self.needs_auth,
            "needs_setup": self.needs_setup,
            **self.extra,
        }


class MailSource(ABC):
    name: str = "base"
    label: str = "Mail"

    @abstractmethod
    def status(self) -> SourceStatus:
        """Cheap, synchronous, never raises. Called on every page load."""

    @abstractmethod
    async def sync(self, days: int | None = None, max_messages: int | None = None) -> dict[str, Any]:
        """Pull recent mail and upsert it. Returns a small report for the UI."""

    # ------------------------------------------------------------------
    # Going back to the original
    #
    # The `emails` row is a *projection*: the fields the list, the ranker and
    # the digest need. Two jobs need more than the projection keeps, and both
    # discovered it the same way -- by not working.
    #
    #   * inline images need the raw MIME parts, which are not in any column;
    #   * replying needs `Message-ID` and `References`, and `_stable_id` hashes
    #     the first into a row id (deliberately -- it has to survive re-sync)
    #     while `conversation_id` truncates the second to 120 characters.
    #
    # So they belong here rather than in a router: *how* to get back to the
    # original is the one thing only the source knows. Apple Mail re-reads the
    # file it came from; a Graph or Gmail message has no file and must ask the
    # API. Both default to "I cannot", so a source that has no way back says so
    # and the caller degrades instead of crashing.
    # ------------------------------------------------------------------

    def inline_part(self, email_id: str, cid: str) -> tuple[str, bytes] | None:
        """`(content_type, bytes)` for one `cid:` image, or None.

        None means "no such image from this source", never an empty payload:
        a zero-byte image makes "not on this machine" indistinguishable from a
        lookup bug, which is exactly how this was misdiagnosed for weeks.

        Implementations must serve `image/*` only. The reading pane renders
        whatever comes back, so anything else here is a way to hand it a script
        from disk.
        """
        return None

    def parent_message(self, email_id: str) -> "ParentMessage | None":
        """Headers for a reply to this message, or None if they cannot be had.

        None must mean the compose pane opens without threading headers rather
        than refusing to open -- a reply that starts a new thread is worse than
        one that threads, and far better than no reply at all.
        """
        return None

    def correspondents(self) -> dict[str, int]:
        """Addresses the user writes to, mapped to how often.

        Replying is a deliberate act, which makes this the least noisy "I care
        about this person" signal a mail source can offer. Optional: a source
        with no access to sent mail returns nothing and the scorer copes.
        """
        return {}


class SourceError(RuntimeError):
    """Something went wrong that the user can act on -- the message is shown verbatim."""
