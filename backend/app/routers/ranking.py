"""The controls for adaptive ranking.

Every endpoint here exists because something in the design needs a knob or an
answer, and none of them exist to expose internals for their own sake:

  GET  /api/ranking              where the boundary is, and what has been learned
  POST /api/ranking/volume       the user-facing control -- a count, not a probability
  POST /api/ranking/epoch        draw the line; learning is off until this is called
  POST /api/ranking/reset        back to the declared priors
  GET  /api/ranking/events       the audit trail, refusals included
  GET  /api/ranking/explain/{id} why one email is where it is
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import db, learning

router = APIRouter(prefix="/api/ranking", tags=["ranking"])


class VolumeBody(BaseModel):
    # How many things belong on today's action list. Nobody knows what 0.63
    # means; everybody knows this.
    target_per_day: int = Field(ge=1, le=200)
    days: int = Field(default=1, ge=1, le=30)


@router.get("")
def state() -> dict:
    thresholds = learning.load_thresholds()
    summary = db.learning_summary()
    return {
        "thresholds": {
            "relevance": thresholds.relevance,
            "action": thresholds.action,
            "moved_today": thresholds.moved_today,
        },
        "target_action_volume": db.get_setting("target_action_volume", "8"),
        "explore_one_in": learning.explore_one_in(),
        # Empty is the honest answer on day one, and it is not a failure: the
        # priors ARE the user's own priority list.
        "learning": {
            "epoch_start": learning.epoch_start(),
            "enabled": bool(learning.epoch_start()),
            **summary,
        },
    }


@router.post("/volume")
def set_volume(body: VolumeBody) -> dict:
    """Solve the action threshold from a volume the user can actually name."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT actionability FROM classifications WHERE actionability IS NOT NULL"
        ).fetchall()
    scores = [float(r["actionability"]) for r in rows]
    if not scores:
        raise HTTPException(
            status_code=409,
            detail="No classified mail yet, so there is nothing to solve the threshold against.",
        )
    updated = learning.set_action_volume(body.target_per_day, scores, body.days)
    return {"relevance": updated.relevance, "action": updated.action, "sampled": len(scores)}


@router.post("/epoch")
def start_epoch() -> dict:
    """Everything before this moment is invisible to learning.

    Deliberately an explicit call rather than something inferred on first run:
    the tables already hold weeks of QA clicking, and a system that decided for
    itself when real use began would have baked that in permanently.
    """
    return {"epoch_start": learning.begin_epoch(), "enabled": True}


@router.post("/reset")
def reset() -> dict:
    """Discard the learned deviation. The declared priorities are untouched --
    this returns the ranking to day one, not to nothing."""
    db.reset_ranking_weights()
    return {"weights": db.ranking_weights()}


@router.get("/events")
def events(limit: int = Query(default=50, ge=1, le=500), applied_only: bool = False) -> dict:
    return {"events": db.learning_events(limit=limit, applied_only=applied_only),
            "summary": db.learning_summary()}


@router.get("/explain/{email_id}")
def explain(email_id: str) -> dict:
    out = learning.explain_email(email_id)
    if out is None:
        raise HTTPException(status_code=404, detail="No classification for that email yet.")
    return out
