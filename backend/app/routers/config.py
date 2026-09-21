"""Settings and priority-list management."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, pipeline, priority
from ..config import DEFAULT_SETTINGS, SECRET_SETTINGS
from ..llm.copilot_provider import CopilotProvider
from ..llm import registry
from ..llm.registry import get_provider, reset_cache
from ..sources.registry import all_status, get_source
from ..transports.registry import all_status as all_transport_status, get_transport
from ..llm.base import ProviderUnavailable

router = APIRouter(prefix="/api", tags=["config"])

# What `db.all_settings()` returns in place of a saved secret.
REDACTED = "********"


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

class SettingsPatch(BaseModel):
    values: dict[str, str]


@router.get("/settings")
def get_settings() -> dict:
    return db.all_settings()


@router.patch("/settings")
def patch_settings(body: SettingsPatch) -> dict:
    unknown = [k for k in body.values if k not in DEFAULT_SETTINGS and k not in SECRET_SETTINGS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown settings: {', '.join(unknown)}")
    for key, value in body.values.items():
        # `all_settings()` hands secrets to the UI as a mask, so a form that
        # round-trips its fields would save the mask *as* the password -- and
        # the next login would fail with the user certain they typed it
        # correctly. Refusing the mask here closes that for every secret field,
        # present and future, rather than trusting each form to remember.
        if key in SECRET_SETTINGS and value == REDACTED:
            continue
        db.set_setting(key, value)
    reset_cache()  # provider or model may have changed
    return db.all_settings()


@router.get("/transports")
def transports() -> dict:
    """Every way out and whether it is usable, so Settings can say "SMTP: needs
    a password" next to "Outlook: not connected".

    Note what is *not* in this file: there is no endpoint that sends anything.
    A message leaves this machine only through a call the user made after
    seeing the message, and that route does not exist yet.
    """
    return {"current": db.get_setting("mail_transport", "none"), "statuses": all_transport_status()}


class TransportTest(BaseModel):
    name: str | None = None


@router.post("/transports/test")
def test_transport(body: TransportTest | None = None) -> dict:
    """Prove the sending settings work before the user needs them to.

    Logs in and finds the right mailbox; **sends nothing and writes nothing**.
    A test that left a stray draft behind, or put a message in someone's inbox,
    would be the first thing the user had to apologise for. Uses the saved
    settings, so Settings must save before testing -- which is also what makes
    the result true of the configuration that will actually be used.
    """
    transport = get_transport((body.name if body else None) or None)
    try:
        result = transport.check()
    except Exception as exc:  # noqa: BLE001 - a test button must never 500
        result = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"transport": transport.name, "mode": transport.mode, **result}


@router.get("/sources")
def sources() -> dict:
    """Every source and whether it is usable right now, so Settings can show
    'Apple Mail: ready, 1,240 messages' next to 'Outlook: no client ID yet'."""
    current = get_source()
    return {"current": current.name, "statuses": all_status()}


@router.post("/settings/test-provider")
async def test_provider() -> dict:
    try:
        provider = get_provider()
    except ProviderUnavailable as exc:
        return {"ok": False, "detail": str(exc)}
    result = await provider.check()
    ok, detail = result
    # A provider that has something structured to say says it; one that returns
    # a plain tuple still works. The English `detail` stays as the fallback for
    # failures nobody anticipated.
    return {"ok": ok, "detail": detail,
            "detail_key": getattr(result, "key", None),
            "detail_vars": getattr(result, "vars", {}) or {},
            "models": getattr(result, "models", []) or [],
            "provider": provider.name, "model": provider.model}


@router.get("/ollama/models")
async def ollama_models() -> dict:
    """What this Mac actually has.

    The app shipped a default of `qwen3.5:9b` and offered a free-text box.
    Someone who pulled `qwen3.5:4b` -- deliberately, because it is the one that
    fits a laptop -- got "Ollama has no model called 'qwen3.5:9b'" and an
    instruction to download six gigabytes they did not need.

    The app should not guess a model name and it must not pick one either: the
    same rule that stops it reaching for a credential stops it choosing what
    runs on someone's machine. So it asks, shows, and lets the user click.
    """
    from ..llm.ollama_provider import OllamaProvider, model_matches

    configured = db.get_setting("ollama_model", "") or ""
    provider = OllamaProvider(configured, db.get_setting("ollama_host", "") or "")
    installed = await provider.available_models()
    return {
        "configured": configured,
        "installed": installed,
        "matches": any(model_matches(configured, m) for m in installed),
    }


@router.get("/settings/copilot-status")
async def copilot_status() -> dict:
    """Lets Settings show 'Copilot: signed in as ...' before the user wonders
    why everything is falling back to structural scoring.

    Guarded: starting the runtime can raise a sign-in prompt, so it must not
    happen to somebody who chose a different provider and never mentioned
    Copilot at all."""
    if registry.configured_provider() != "copilot":
        return {"signed_in": False, "selected": False, "models": []}
    provider = CopilotProvider(db.get_setting("copilot_model", "auto"))
    status = await provider.auth_status()
    status["models"] = await provider.list_models() if status.get("signed_in") else []
    await provider.aclose()
    return status


# --------------------------------------------------------------------------
# priorities
# --------------------------------------------------------------------------

class PriorityIn(BaseModel):
    topic: str
    status: str = "active"
    weight: int = 10
    note: str | None = None
    source: str = "user"


class PriorityPatch(BaseModel):
    status: str | None = None
    weight: int | None = None
    note: str | None = None


VALID_STATUS = {"active", "low_care", "dismissed"}


@router.get("/priorities")
def list_priorities() -> dict:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM priorities ORDER BY status, weight DESC, topic").fetchall()
    return {"items": [dict(r) for r in rows]}


@router.post("/priorities")
def add_priority(body: PriorityIn) -> dict:
    topic = body.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="Topic cannot be empty.")
    if body.status not in VALID_STATUS:
        raise HTTPException(status_code=400, detail=f"Status must be one of {sorted(VALID_STATUS)}.")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO priorities (topic, status, weight, source, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(topic) DO UPDATE SET "
            "status=excluded.status, weight=excluded.weight, note=excluded.note",
            (topic, body.status, body.weight, body.source, body.note, db.now_iso()),
        )
        conn.commit()
    pipeline.rescore_all()
    return list_priorities()


@router.patch("/priorities/{priority_id}")
def update_priority(priority_id: int, body: PriorityPatch) -> dict:
    if body.status is not None and body.status not in VALID_STATUS:
        raise HTTPException(status_code=400, detail=f"Status must be one of {sorted(VALID_STATUS)}.")
    fields, params = [], []
    for column, value in (("status", body.status), ("weight", body.weight), ("note", body.note)):
        if value is not None:
            fields.append(f"{column} = ?")
            params.append(value)
    if not fields:
        return list_priorities()
    with db.connect() as conn:
        cursor = conn.execute(
            f"UPDATE priorities SET {', '.join(fields)} WHERE id = ?", (*params, priority_id)
        )
        conn.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Priority not found.")
    pipeline.rescore_all()
    return list_priorities()


@router.delete("/priorities/{priority_id}")
def delete_priority(priority_id: int) -> dict:
    with db.connect() as conn:
        conn.execute("DELETE FROM priorities WHERE id = ?", (priority_id,))
        conn.commit()
    pipeline.rescore_all()
    return list_priorities()


@router.get("/priorities/suggestions")
def suggestions(days: int = 60) -> dict:
    """On demand only -- the spec is explicit that this is never a forced prompt."""
    return {"items": priority.suggest_topics(days=days)}
