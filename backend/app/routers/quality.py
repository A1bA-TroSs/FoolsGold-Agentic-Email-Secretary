"""Tester quality check (roadmap A5). See app/quality.py.

  POST /api/quality/sample   pick the emails to label (resumable)
  GET  /api/quality/next     the next email to label, without the app's answer
  POST /api/quality/label    save one label
  POST /api/quality/undo     take back a saved label
  GET  /api/quality/score    per-language numbers, Apple Priority beside them
  GET  /api/quality/export   the numbers-only score card a tester sends back
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import quality

router = APIRouter(prefix="/api/quality", tags=["quality"])


class SampleIn(BaseModel):
    n: int = Field(default=quality.DEFAULT_N, ge=1, le=500)


class LabelIn(BaseModel):
    email_id: str
    bucket: str
    addressed: bool = False
    deadline: str | None = None


@router.post("/sample")
def sample(body: SampleIn) -> dict:
    return quality.sample(body.n)


@router.get("/next")
def next_item() -> dict:
    return {"item": quality.next_item(), **quality.progress()}


@router.post("/label")
def label(body: LabelIn) -> dict:
    try:
        quality.save_label(body.email_id, body.bucket, body.addressed, body.deadline)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Not in the quality-check sample.") from exc
    return quality.progress()


class UndoIn(BaseModel):
    email_id: str


@router.post("/undo")
def undo(body: UndoIn) -> dict:
    try:
        quality.undo(body.email_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No saved label to take back.") from exc
    return quality.progress()


@router.get("/score")
def score() -> dict:
    return quality.score()


@router.get("/export")
def export() -> dict:
    return quality.scorecard()
