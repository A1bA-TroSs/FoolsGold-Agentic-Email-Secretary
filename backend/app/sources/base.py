"""Mail source contract.

A source's only job is to put normalised rows into the `emails` table. Where
they came from -- Microsoft Graph over the network, or Apple Mail's local store
on this Mac -- is invisible to classification, scoring, the digest and the UI.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


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

    def correspondents(self) -> dict[str, int]:
        """Addresses the user writes to, mapped to how often.

        Replying is a deliberate act, which makes this the least noisy "I care
        about this person" signal a mail source can offer. Optional: a source
        with no access to sent mail returns nothing and the scorer copes.
        """
        return {}


class SourceError(RuntimeError):
    """Something went wrong that the user can act on -- the message is shown verbatim."""
