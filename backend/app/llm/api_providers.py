"""Providers you drive with your own API key.

This is the path the app is built around: your key, your account, your bill.
No shared or bundled credential exists anywhere in this project -- if you have
not entered a key, the app ranks mail on structural signals and says so.

Three things here exist because a stranger's first hour is not the happy path:

* **Retries.** A brand-new key on the lowest tier, asked to classify a few
  hundred messages in batches, will be rate limited. That is the expected
  case, not an edge case, so 429 and 5xx back off and try again rather than
  failing the batch.
* **Truncation detection.** A reply cut off at the token limit is still a
  valid HTTP 200 carrying unparseable JSON, which is indistinguishable from
  "your key is broken" unless somebody checks. We check.
* **Sanitised errors.** Upstream error bodies reach the UI, and some gateways
  include the request headers in theirs. Nothing here forwards a raw body, and
  every message is scrubbed of anything key-shaped on the way out.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from .base import LLMProvider, ProviderUnavailable

_TIMEOUT = httpx.Timeout(120.0, connect=15.0)

# Enough for a batch of ten classifications carrying five extracted tasks each.
# The old 4096 truncated a full batch, and a truncated reply is not something
# the user could have diagnosed -- it looked exactly like a rejected key.
_MAX_TOKENS = 8192

_RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4
_BACKOFF_BASE = 1.5

# Anything credential-shaped, whoever wrote it.
_KEYISH = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|Bearer\s+\S+|[A-Za-z0-9_\-]{32,})"
)


def scrub(text: str, *secrets: str) -> str:
    """Keep keys out of logs, banners and error strings.

    The provider's own key is redacted by value; anything else key-shaped is
    redacted by pattern, because the text being quoted was written by someone
    else's gateway and may contain headers we never sent."""
    out = text or ""
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "***")
    return _KEYISH.sub("***", out)


def _message_from(body: str) -> str:
    """The human-readable part of an upstream error, and nothing else.

    Providers return `{"error": {"message": ...}}`; proxies return HTML. Take
    the sentence if there is one and never forward the raw body, which is
    where echoed request headers would be."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("type") or "")[:200]
    if isinstance(err, str):
        return err[:200]
    if data.get("message"):
        return str(data["message"])[:200]
    return ""


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    header = resp.headers.get("retry-after")
    if header:
        try:
            return min(float(header), 30.0)
        except ValueError:
            pass
    return _BACKOFF_BASE ** attempt


class _HttpProvider(LLMProvider):
    """Shared request loop. Subclasses describe one request and read one reply."""

    label = "Provider"

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()

    # -- implemented per provider -------------------------------------------
    def _request(self, system: str, user: str) -> dict[str, Any]:
        raise NotImplementedError

    def _read(self, payload: dict[str, Any]) -> str:
        raise NotImplementedError

    # -----------------------------------------------------------------------
    def _fail(self, status: int, body: str) -> ProviderUnavailable:
        detail = scrub(_message_from(body), self.api_key)
        if status in (401, 403):
            hint = f"{self.label} rejected the key ({status})."
        elif status == 404:
            hint = f"{self.label} does not recognise the model '{self.model}' ({status})."
        elif status == 429:
            hint = f"{self.label} rate limit reached; it was still refusing after retries."
        else:
            hint = f"{self.label} returned {status}."
        return ProviderUnavailable(f"{hint} {detail}".strip())

    async def complete(self, system: str, user: str) -> str:
        if not self.api_key:
            raise ProviderUnavailable(
                f"No {self.label} API key set. Add one in Settings, or turn AI off."
            )

        last: ProviderUnavailable | None = None
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            for attempt in range(_MAX_ATTEMPTS):
                delay: float | None = None
                try:
                    resp = await client.post(**self._request(system, user))
                except httpx.TimeoutException:
                    last = ProviderUnavailable(f"{self.label} timed out.")
                    delay = _BACKOFF_BASE ** attempt
                except httpx.HTTPError as exc:
                    last = ProviderUnavailable(
                        f"Could not reach {self.label}: {scrub(str(exc), self.api_key)}"
                    )
                    delay = _BACKOFF_BASE ** attempt
                else:
                    if resp.status_code < 400:
                        try:
                            payload = resp.json()
                        except ValueError:
                            raise ProviderUnavailable(
                                f"{self.label} replied with something that was not JSON. "
                                "Check whether a proxy or a network sign-in page is in the way."
                            ) from None
                        return self._read(payload)

                    # A refused key or an unknown model will not fix itself.
                    if resp.status_code not in _RETRY_STATUSES:
                        raise self._fail(resp.status_code, resp.text)
                    last = self._fail(resp.status_code, resp.text)
                    delay = _retry_after(resp, attempt)

                if attempt < _MAX_ATTEMPTS - 1 and delay is not None:
                    await asyncio.sleep(delay)

        raise last or ProviderUnavailable(f"{self.label} did not answer.")


class AnthropicProvider(_HttpProvider):
    name = "anthropic"
    label = "Anthropic"

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-5") -> None:
        super().__init__(api_key, model or "claude-sonnet-4-5")

    def _request(self, system: str, user: str) -> dict[str, Any]:
        return {
            "url": "https://api.anthropic.com/v1/messages",
            "headers": {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            "json": {
                "model": self.model,
                "max_tokens": _MAX_TOKENS,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        }

    def _read(self, payload: dict[str, Any]) -> str:
        if payload.get("stop_reason") == "max_tokens":
            raise ProviderUnavailable(
                "The reply was cut off at the token limit. Lower 'emails per AI request' "
                "in Settings and try again."
            )
        blocks = payload.get("content") or []
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


class OpenAIProvider(_HttpProvider):
    name = "openai"
    label = "OpenAI"

    def __init__(self, api_key: str, model: str = "gpt-4o-mini",
                 base_url: str | None = None) -> None:
        super().__init__(api_key, model or "gpt-4o-mini")
        # Configurable, so the same provider serves Azure, OpenRouter, a local
        # llama.cpp server, or anything else speaking this API. This used to be
        # a constructor argument that nothing ever passed.
        self.base_url = (base_url or "").strip().rstrip("/") or "https://api.openai.com/v1"

    def _request(self, system: str, user: str) -> dict[str, Any]:
        return {
            "url": f"{self.base_url}/chat/completions",
            "headers": {"Authorization": f"Bearer {self.api_key}"},
            "json": {
                "model": self.model,
                "max_tokens": _MAX_TOKENS,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        }

    def _read(self, payload: dict[str, Any]) -> str:
        choices = payload.get("choices") or []
        if not choices:
            raise ProviderUnavailable(
                "The model returned no answer at all, which usually means the request "
                "was refused rather than that anything is misconfigured."
            )
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            raise ProviderUnavailable(
                "The reply was cut off at the token limit. Lower 'emails per AI request' "
                "in Settings and try again."
            )
        content = (choice.get("message") or {}).get("content")
        if not content:
            raise ProviderUnavailable(
                "The model returned an empty answer, which usually means the request "
                "was refused rather than that anything is misconfigured."
            )
        return content
