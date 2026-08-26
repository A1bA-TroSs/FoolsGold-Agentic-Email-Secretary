"""Key-based providers. Same contract as Copilot, different billing model:
per-token and predictable, at the cost of you holding an API key."""
from __future__ import annotations

import httpx

from .base import LLMProvider, ProviderUnavailable

_TIMEOUT = httpx.Timeout(120.0, connect=15.0)


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-5") -> None:
        self.api_key = (api_key or "").strip()
        self.model = model

    async def complete(self, system: str, user: str) -> str:
        if not self.api_key:
            raise ProviderUnavailable("No Anthropic API key set. Add one in Settings.")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.model,
                    "max_tokens": 4096,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                },
            )
        if resp.status_code >= 400:
            raise ProviderUnavailable(f"Anthropic {resp.status_code}: {resp.text[:300]}")
        blocks = resp.json().get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str = "gpt-4o-mini", base_url: str | None = None) -> None:
        self.api_key = (api_key or "").strip()
        self.model = model
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")

    async def complete(self, system: str, user: str) -> str:
        if not self.api_key:
            raise ProviderUnavailable("No OpenAI API key set. Add one in Settings.")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
        if resp.status_code >= 400:
            raise ProviderUnavailable(f"OpenAI {resp.status_code}: {resp.text[:300]}")
        choices = resp.json().get("choices") or []
        if not choices:
            raise ProviderUnavailable("OpenAI returned no choices.")
        return choices[0].get("message", {}).get("content", "") or ""
