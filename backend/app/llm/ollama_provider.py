"""A local model, over Ollama's HTTP API.

Why this provider is the one that matters
-----------------------------------------
Every other provider sends the user's entire mailbox to somebody else's
computer. This one does not: the mail never leaves the Mac. For an app whose
whole job is reading a person's correspondence, that is a stronger argument than
cost, and it is the argument that made a local model worth the work.

What it does NOT do
-------------------
It does not personalise anything. Relevance lives in `relevance.py` -- embeddings,
an online weight vector, an adjustable threshold -- because preference moves and
frozen weights cannot follow it. This provider answers one stable question about
each email: *what does this text say, and what does it ask for?* The same email
gets the same answer in January and in June, which is correct, because the email
did not change.

One model, not several
----------------------
The heterogeneous-SLM argument is real and does not apply here yet: after the
two-axis split there is exactly one generative job left, the machine has 24GB
shared with Electron and Python, and swapping models costs 1-3s per switch for a
second job that runs once a day. `DEFAULTS` below therefore names one model, and
the seam for a second one is a settings key rather than a redesign.

Structured output
-----------------
`format` takes a JSON schema and Ollama constrains decoding to it, so malformed
JSON becomes structurally impossible rather than merely unlikely. That matters
more than prompt wording: the build notes already list "LLM-response parsing" as
one of the parts most likely to silently misbehave.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from .. import relevance
from .base import (
    CLASSIFY_SYSTEM,
    DIGEST_SYSTEM,
    AgendaItem,
    Classification,
    LLMProvider,
    ProviderUnavailable,
    build_classify_prompt,
    build_digest_prompt,
    parse_agenda,
    parse_classifications,
)

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.5:9b"

# Ollama keeps a model resident for 5 minutes by default. Classification arrives
# in bursts with long gaps, so every burst would pay a reload. Thirty minutes
# costs nothing while the app is idle -- the memory is reclaimed either way --
# and removes the stall from the path the user actually waits on.
KEEP_ALIVE = "30m"

# A local 9B on a laptop is not fast. The first call also pays the load, which
# is seconds on its own, so the timeout has to cover load + generation for a
# whole batch or the first sync of the day looks like a failure.
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 300.0

# "" is in the enum on purpose: the model must be able to say "none of these
# fit". Without it, constrained decoding would force a category onto every
# email, and a forced label is worse than no label -- it is a wrong one the
# user's clicks would then attach a weight to.
CATEGORY_VALUES = ("", *relevance.CATEGORIES)

CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    # An enum, not a string. This is the single highest-value
                    # line in the file: it makes "actoin" and "Action needed"
                    # and a paragraph of reasoning impossible to emit, instead
                    # of things the parser has to survive.
                    "bucket": {"type": "string", "enum": ["action", "fyi", "noise"]},
                    "deadline": {"type": ["string", "null"]},
                    "rationale": {"type": "string"},
                    "matched": {"type": "array", "items": {"type": "string"}},
                    # An enum again, and required again. `matched` taught this
                    # the hard way: an optional field under constrained
                    # decoding is one the model can decline to emit, and a
                    # missing key is indistinguishable from "none applied".
                    "category": {"type": "string", "enum": list(CATEGORY_VALUES)},
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "due": {"type": ["string", "null"]},
                            },
                            "required": ["title"],
                        },
                    },
                },
                # `matched` is required, and that is load-bearing rather than
                # tidy. Under constrained decoding an optional field is one the
                # model may simply not emit -- and this one did not, on the
                # first real run, so the topic feature read zero and the whole
                # declared priority list contributed nothing to relevance.
                # A required empty array is an answer; a missing key is silence
                # that looks identical to "nothing matched".
                "required": ["id", "bucket", "matched", "category"],
            },
        }
    },
    "required": ["results"],
}

DIGEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "agenda": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["id", "note"],
            },
        },
    },
    "required": ["headline", "agenda"],
}


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, model: str = "", host: str = "") -> None:
        self.model = model or DEFAULT_MODEL
        self.host = (host or DEFAULT_HOST).rstrip("/")

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    async def _chat(self, system: str, user: str, schema: dict[str, Any] | None = None) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # One response, not a stream. The caller wants a whole batch parsed,
            # and assembling a stream here would buy nothing but a partial-read
            # failure mode.
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "options": {
                # Triage is a judgement to be made the same way twice, not a
                # creative act. Temperature 0 also makes a disagreement between
                # two runs mean something.
                "temperature": 0,
                "num_ctx": 8192,
            },
            # Qwen3.x reasons by default. For a classification that needs no
            # reasoning trace it is pure latency and pure token burn on a
            # machine that is also running the user's actual work.
            "think": False,
        }
        if schema:
            payload["format"] = schema

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT)
            ) as client:
                response = await client.post(f"{self.host}/api/chat", json=payload)
        except httpx.ConnectError as exc:
            raise ProviderUnavailable(
                f"No local model server at {self.host}. Start Ollama, or change the "
                f"host in Settings."
            ) from exc
        except httpx.ReadTimeout as exc:
            raise ProviderUnavailable(
                f"{self.model} did not answer within {READ_TIMEOUT:.0f}s. A first call "
                f"also loads the model; try a smaller one, or reduce the batch size."
            ) from exc

        if response.status_code == 404:
            raise ProviderUnavailable(
                f"Ollama has no model called '{self.model}'. Pull it first: "
                f"ollama pull {self.model}"
            )
        if response.status_code >= 400:
            raise ProviderUnavailable(
                f"Ollama returned {response.status_code}: {response.text[:200]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderUnavailable(
                f"Ollama returned something that is not JSON: {response.text[:200]}"
            ) from exc

        # `done_reason: length` means the schema was still being satisfied when
        # the token budget ran out, so the JSON is truncated. Reported rather
        # than handed to the parser, because a half-object parses into a
        # confident wrong answer for the emails that happened to come first.
        if body.get("done_reason") == "length":
            raise ProviderUnavailable(
                f"{self.model} hit its token limit mid-answer. Reduce "
                f"'Emails per AI request' in Settings."
            )

        return (body.get("message") or {}).get("content") or ""

    async def complete(self, system: str, user: str) -> str:
        return await self._chat(system, user)

    # ------------------------------------------------------------------
    # the two real jobs, each with its schema
    # ------------------------------------------------------------------

    async def classify_batch(
        self, emails: list[dict[str, Any]], priorities: list[str]
    ) -> list[Classification]:
        if not emails:
            return []
        raw = await self._chat(
            CLASSIFY_SYSTEM, build_classify_prompt(emails, priorities), CLASSIFY_SCHEMA
        )
        # The schema wraps the list in an object because Ollama constrains to a
        # schema with a named root far more reliably than to a bare array. The
        # shared parser wants the array, so unwrap here rather than teaching
        # every other provider about this one's envelope.
        return parse_classifications(_unwrap(raw, "results"), [e["id"] for e in emails])

    async def summarize(
        self, emails: list[dict[str, Any]], priorities: list[str], language: str = "en"
    ) -> tuple[str, list[AgendaItem]]:
        if not emails:
            return "Nothing in the inbox needs you today.", []
        raw = await self._chat(
            DIGEST_SYSTEM, build_digest_prompt(emails, priorities, language), DIGEST_SCHEMA
        )
        return parse_agenda(raw, [e["id"] for e in emails])

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    async def check(self) -> tuple[bool, str]:
        """Answer the question the user is actually asking in Settings: is a
        local model there, and is it the one I chose?

        Deliberately does NOT generate. A generation check on a 9B that has to
        load first takes tens of seconds and reports a timeout as a
        misconfiguration.
        """
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=CONNECT_TIMEOUT)
            ) as client:
                response = await client.get(f"{self.host}/api/tags")
        except httpx.HTTPError as exc:
            return False, f"No local model server at {self.host}: {exc}"

        if response.status_code >= 400:
            return False, f"Ollama returned {response.status_code}"
        try:
            models = [m.get("name", "") for m in (response.json().get("models") or [])]
        except ValueError:
            return False, "Ollama's model list was not JSON"

        if not models:
            return False, f"Ollama is running but has no models. Try: ollama pull {self.model}"
        # `qwen3.5:9b` and `qwen3.5:9b-instruct-q4_K_M` are the same choice to a
        # user who typed the short name.
        if not any(m == self.model or m.startswith(self.model.split(":")[0]) for m in models):
            return False, (
                f"'{self.model}' is not pulled. Available: {', '.join(models[:5])}. "
                f"Try: ollama pull {self.model}"
            )
        return True, f"{self.model} ready on {self.host}"

    async def available_models(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=CONNECT_TIMEOUT)) as client:
                response = await client.get(f"{self.host}/api/tags")
            return [m.get("name", "") for m in (response.json().get("models") or [])]
        except (httpx.HTTPError, ValueError):
            return []


def _unwrap(raw: str, key: str) -> str:
    """Return the inner array as JSON text, or the original string untouched.

    Untouched rather than raising: a model that ignored the envelope and emitted
    the bare array is still giving the right answer, and the shared parser
    already handles that shape. Failing here would turn a cosmetic difference
    into an outage.
    """
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    if isinstance(parsed, dict) and isinstance(parsed.get(key), list):
        return json.dumps(parsed[key])
    return raw
