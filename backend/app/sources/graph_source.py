"""Microsoft Graph, wrapped in the MailSource contract."""
from __future__ import annotations

from typing import Any

from ..graph import auth as graph_auth
from ..graph import sync as graph_sync
from .base import MailSource, SourceError, SourceStatus


class GraphSource(MailSource):
    name = "graph"
    label = "Outlook (Microsoft account)"

    def status(self) -> SourceStatus:
        try:
            info = graph_auth.status()
        except Exception as exc:  # noqa: BLE001
            return SourceStatus(ready=False, detail=str(exc), needs_setup=True)

        if not info.get("configured"):
            return SourceStatus(
                ready=False,
                detail="No Microsoft Entra client ID set yet.",
                needs_setup=True,
            )
        if not info.get("signed_in"):
            return SourceStatus(ready=False, detail="Not signed in to Outlook.", needs_auth=True)
        return SourceStatus(ready=True, account=info.get("account"))

    async def sync(self, days: int | None = None, max_messages: int | None = None) -> dict[str, Any]:
        try:
            return await graph_sync.sync_inbox(days=days, max_messages=max_messages)
        except graph_auth.NotConfigured as exc:
            raise SourceError(str(exc)) from exc
        except graph_auth.AuthError as exc:
            raise SourceError(str(exc)) from exc
        except graph_sync.GraphError as exc:
            raise SourceError(str(exc)) from exc
