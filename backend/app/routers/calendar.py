"""Calendar and checklist endpoints."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

from .. import db, pipeline, planner

router = APIRouter(prefix="/api/calendar", tags=["calendar"])


def _valid_day(value: str) -> str:
    """A date has to be a real day on the real calendar. Checking the shape
    only (`\\d{4}-\\d{2}-\\d{2}`) lets 2026-02-30 through, and it then fails
    much later somewhere far less obvious."""
    day = planner.parse_day(value)
    if day is None:
        raise ValueError("due_date must be a real date in YYYY-MM-DD form")
    return day.isoformat()


@router.get("/month")
def month(
    year: int = Query(..., ge=1970, le=2200),
    month: int = Query(..., ge=1, le=12),
    week_starts_on: int = Query(0, ge=0, le=6, description="0 = Monday"),
) -> dict:
    return planner.month(year, month, week_starts_on)


@router.get("/agenda")
def agenda(day: str | None = None) -> dict:
    target = planner.parse_day(day) if day else date.today()
    if target is None:
        raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD")
    return planner.agenda(target)


class TaskIn(BaseModel):
    title: str
    due_date: str
    note: str | None = None

    @field_validator("due_date")
    @classmethod
    def _check_due(cls, value: str) -> str:
        return _valid_day(value)

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str) -> str:
        title = (value or "").strip()
        if not title:
            raise ValueError("title cannot be empty")
        return title[:300]


class TaskPatch(BaseModel):
    title: str | None = None
    due_date: str | None = None
    note: str | None = None
    status: str | None = None

    @field_validator("due_date")
    @classmethod
    def _check_due(cls, value: str | None) -> str | None:
        return _valid_day(value) if value is not None else None

    @field_validator("status")
    @classmethod
    def _check_status(cls, value: str | None) -> str | None:
        if value is not None and value not in {"open", "done"}:
            raise ValueError("status must be open or done")
        return value


@router.post("/tasks")
def create_task(body: TaskIn) -> dict:
    return db.add_task(body.title, body.due_date, body.note)


@router.patch("/tasks/{task_id}")
def patch_task(task_id: int, body: TaskPatch) -> dict:
    row = db.update_task(
        task_id, title=body.title, due_date=body.due_date, note=body.note, status=body.status
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return row


@router.delete("/tasks/{task_id}")
def remove_task(task_id: int) -> dict:
    if not db.delete_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found.")
    return {"id": task_id, "removed": True}


class DoneIn(BaseModel):
    done: bool = True


@router.post("/entries/{kind}/{ident}/done")
def set_done(kind: str, ident: str, body: DoneIn) -> dict:
    try:
        result = planner.set_done(kind, ident, body.done)
    except KeyError:
        raise HTTPException(status_code=404, detail="Entry not found.") from None
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if kind == "email":
        pipeline.rescore_all()
    return result


@router.delete("/entries/{kind}/{ident}")
def remove_entry(kind: str, ident: str) -> dict:
    """Taking something off the calendar. Reversible for both kinds: a task is
    tombstoned and an email's due date is hidden, and both land in the removed
    box. The message itself is untouched either way -- it is still mail, and it
    still belongs in the inbox."""
    try:
        return planner.remove(kind, ident)
    except KeyError:
        raise HTTPException(status_code=404, detail="Entry not found.") from None
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/removed")
def removed() -> dict:
    """Everything the user took off the calendar, newest first, so it can come
    back. Muted senders are undone in the muted box instead."""
    return {"items": planner.removed()}


@router.post("/entries/{kind}/{ident}/restore")
def restore_entry(kind: str, ident: str) -> dict:
    try:
        return planner.restore(kind, ident)
    except KeyError:
        raise HTTPException(status_code=404, detail="Nothing to restore.") from None
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/removed/{kind}/{ident}")
def purge_entry(kind: str, ident: str) -> dict:
    """Permanent. Only reachable from the removed box, and only for tasks."""
    try:
        return planner.purge(kind, ident)
    except KeyError:
        raise HTTPException(status_code=404, detail="Entry not found.") from None
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
