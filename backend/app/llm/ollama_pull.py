"""Download an Ollama model from inside the app (roadmap F5).

Settings used to say "In a terminal: ollama pull <model>", which is the one
step of the private, local path that needed a terminal (viability gate G2).
This drives Ollama's own `POST /api/pull` instead and keeps its progress where
the UI can read it.

Rules:
  * Started only by the user's click (the POST endpoint); nothing here runs on
    its own, and a finished pull does not change `ollama_model` -- the model
    list refreshes and the user picks, as before ("shown, never chosen").
  * One pull at a time. A second request while one runs is refused.
  * The name is checked before anything is sent.
  * Every way it can end -- success, an error line from Ollama, a dropped
    connection, an unreachable host -- leaves a final state, never a stuck
    "downloading".
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import httpx

from .ollama_provider import CONNECT_TIMEOUT, DEFAULT_HOST

# Ollama model names: an optional namespace path, a name and a tag, made of
# lower-case letters, digits and . _ - (the registry's own alphabet).
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*(:[a-z0-9][a-z0-9._-]*)?$")
MAX_NAME = 120

# The two sizes offered (Danny, 2026-11-02: "Two sizes, suggest by Mac
# memory"). Sizes are approximate 4-bit downloads; `min_gb` is the memory a
# Mac needs for the model to run alongside everything else.
SUGGESTED = [
    {"model": "qwen3.5:4b", "size_gb": 3, "min_gb": 8},
    {"model": "qwen3.5:9b", "size_gb": 6, "min_gb": 16},
]


def valid_name(name: str) -> bool:
    name = (name or "").strip()
    return 0 < len(name) <= MAX_NAME and ".." not in name and bool(_NAME.match(name))


def memory_gb() -> float | None:
    """This machine's physical memory, or None when it cannot be read."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3
    except (ValueError, OSError, AttributeError):
        return None


def suggestions() -> list[dict[str, Any]]:
    """The offered models, each marked `fits` against this machine's memory.

    The largest one that fits is marked `suggested`; nothing is chosen."""
    mem = memory_gb()
    out = [{**s, "fits": mem is None or mem + 0.5 >= s["min_gb"], "suggested": False} for s in SUGGESTED]
    fitting = [s for s in out if s["fits"]]
    if fitting:
        fitting[-1]["suggested"] = True
    return out


def _idle() -> dict[str, Any]:
    return {"model": "", "status": "", "completed": 0, "total": 0,
            "done": False, "error": "", "error_kind": "", "running": False}


def error_kind(text: str) -> str:
    """Sort Ollama's error text into the few cases the UI explains in plain
    words. Anything else is shown as Ollama said it."""
    low = (text or "").lower()
    if "no space left" in low or ("disk" in low and "space" in low):
        return "disk"
    if ("file does not exist" in low or "not found" in low
            or "manifest" in low or "unknown" in low):
        return "unknown_model"
    return "other"


def _fail(text: str, kind: str = "") -> None:
    text = (text or "").strip()[:300]
    _state.update(error=text, error_kind=kind or error_kind(text))


_state: dict[str, Any] = _idle()
_task: asyncio.Task | None = None


def state() -> dict[str, Any]:
    return dict(_state)


def running() -> bool:
    return bool(_state.get("running"))


async def _run(host: str, model: str) -> None:
    url = f"{(host or DEFAULT_HOST).rstrip('/')}/api/pull"
    # No read timeout: a multi-gigabyte layer can go quiet while Ollama
    # verifies it. The connect timeout still catches a missing server.
    timeout = httpx.Timeout(None, connect=CONNECT_TIMEOUT)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json={"model": model, "stream": True}) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")[:200]
                    _fail(f"Ollama returned {resp.status_code}: {body}")
                    return
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("error"):
                        _fail(str(msg["error"]))
                        return
                    status = str(msg.get("status") or "")
                    _state["status"] = status
                    if msg.get("total"):
                        _state["total"] = int(msg["total"])
                        _state["completed"] = int(msg.get("completed") or 0)
                    if status == "success":
                        if _state["total"]:
                            _state["completed"] = _state["total"]
                        _state["done"] = True
                        return
        if not _state["done"] and not _state["error"]:
            _fail("The download stopped before Ollama said it was finished.", "other")
    except httpx.ConnectError:
        _fail(f"No Ollama server at {host or DEFAULT_HOST}. Is Ollama running?", "no_server")
    except httpx.HTTPError as exc:
        _fail(f"The download was interrupted: {exc}", "other")
    except Exception as exc:  # noqa: BLE001 - a background task must always end in a state
        _fail(f"The download failed: {exc}", "other")
    finally:
        _state["running"] = False


def start(host: str, model: str) -> dict[str, Any]:
    """Begin a pull in the background. Raises ValueError for a bad name and
    RuntimeError while another pull runs."""
    global _task
    model = (model or "").strip()
    if not valid_name(model):
        raise ValueError("Not a model name Ollama would accept.")
    if running():
        raise RuntimeError(f"Already downloading {_state['model']}.")
    _state.clear()
    _state.update(_idle(), model=model, status="starting", running=True)
    _task = asyncio.get_running_loop().create_task(_run(host, model))
    return state()
