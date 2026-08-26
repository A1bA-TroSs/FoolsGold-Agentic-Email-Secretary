"""Settings and priority-list management."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, pipeline, priority
from ..config import DEFAULT_SETTINGS, SECRET_SETTINGS
from ..llm.copilot_provider import CopilotProvider
from ..llm.registry import get_provider, reset_cache
from ..sources.registry import all_status, get_source
from ..llm.base import ProviderUnavailable

router = APIRouter(prefix="/api", tags=["config"])


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
        db.set_setting(key, value)
    reset_cache()  # provider or model may have changed
    return db.all_settings()


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
    ok, detail = await provider.check()
    return {"ok": ok, "detail": detail, "provider": provider.name, "model": provider.model}


@router.get("/settings/copilot-status")
async def copilot_status() -> dict:
    """Lets Settings show 'Copilot: signed in as ...' before the user wonders
    why everything is falling back to structural scoring."""
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
