"""Inbox, detail, sync and digest endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .. import db, pipeline
from ..sources.base import SourceError
from ..sources.registry import get_source

router = APIRouter(prefix="/api/mail", tags=["mail"])

_LIST_COLUMNS = (
    "e.id, e.subject, e.from_name, e.from_address, e.received_at, e.is_read, "
    "e.is_answered, e.is_flagged, e.has_attachments, e.importance, e.body_preview, "
    "c.bucket, c.deadline, c.score, c.matched, c.rationale, c.source, c.explored, "
    "c.reason_code, c.reason_arg, c.category, "
    "f.verdict, f.snooze_until, f.created_at AS decided_at, h.color AS highlight"
)

# Highlights are a property of the correspondent, so they join in exactly where
# muting is consulted -- one source of truth, and the mailbox and the calendar
# cannot disagree about who is coloured.
_HIGHLIGHT_JOIN = "LEFT JOIN highlighted_senders h ON h.address = e.from_address"


@router.get("")
def list_mail(
    bucket: str | None = Query(None, description="action | fyi | noise"),
    search: str | None = None,
    sort: str = Query("priority", pattern="^(priority|date|decided)$"),
    muted: bool = Query(False, description="show only mail from muted senders"),
    verdict: str | None = Query(None, description="show only mail carrying this verdict"),
    limit: int = Query(200, le=1000),
) -> dict:
    where, params = [], []
    # A verdict filter is a view of the user's own decisions, so it is the one
    # place the mute filter does not apply. Muting a correspondent tomorrow must
    # not erase the fact that you finished something of theirs today -- the
    # completed box would quietly lose rows, which is precisely the failure the
    # box exists to prevent.
    if verdict:
        if verdict not in db.VALID_VERDICTS:
            raise HTTPException(status_code=400, detail=f"verdict must be one of {sorted(db.VALID_VERDICTS)}")
        where.append("f.verdict = ?")
        params.append(verdict)
    else:
        # Muted senders are hidden everywhere except the muted box itself.
        where.append(
            "e.from_address IN (SELECT address FROM muted_senders)" if muted
            else "e.from_address NOT IN (SELECT address FROM muted_senders)"
        )
    if bucket:
        where.append("c.bucket = ?")
        params.append(bucket)
    if search:
        where.append("(e.subject LIKE ? OR e.from_name LIKE ? OR e.from_address LIKE ? OR e.body_text LIKE ?)")
        params.extend([f"%{search}%"] * 4)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    # Pinned mail sorts to the top as a group rather than merely scoring higher.
    # A +60 bump left a pinned item sitting fourth, which is not what the word
    # "pin" promises -- it is an explicit override, so it overrides. Score still
    # orders items *within* the pinned group.
    if sort == "priority":
        order = ("CASE WHEN f.verdict = 'pinned' THEN 0 ELSE 1 END, "
                 "COALESCE(c.score, 0) DESC, e.received_at DESC")
    elif sort == "decided":
        # Most recently decided first: the thing you just ticked off by mistake
        # is the thing you came here to undo, so it must be the first row.
        order = "COALESCE(f.created_at, e.received_at) DESC"
    else:
        order = "e.received_at DESC"

    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT {_LIST_COLUMNS} FROM emails e "
            f"LEFT JOIN classifications c ON c.email_id = e.id "
            f"LEFT JOIN feedback f ON f.email_id = e.id "
            f"{_HIGHLIGHT_JOIN} "
            f"{clause} ORDER BY {order} LIMIT ?",
            (*params, limit),
        ).fetchall()
        counts = {
            r["bucket"] or "unclassified": r["n"]
            for r in conn.execute(
                "SELECT c.bucket, COUNT(*) n FROM emails e "
                "LEFT JOIN classifications c ON c.email_id = e.id GROUP BY c.bucket"
            ).fetchall()
        }

    now = datetime.now(timezone.utc)
    items = []
    for row in rows:
        item = dict(row)
        item["matched"] = [m for m in (item.get("matched") or "").split(",") if m]
        # A snooze that has expired is no longer a snooze.
        if item.get("verdict") == "snoozed" and item.get("snooze_until"):
            try:
                if datetime.fromisoformat(item["snooze_until"]) <= now:
                    item["verdict"] = None
                    item["snooze_until"] = None
            except ValueError:
                item["verdict"] = None
        items.append(item)
    return {"items": items, "counts": counts, "ai": pipeline.ai_status(),
            "sync": sync_state()}


class FeedbackIn(BaseModel):
    verdict: str                    # done | pinned | not_important | snoozed
    snooze_hours: int | None = None


@router.post("/{email_id}/feedback")
def set_feedback(email_id: str, body: FeedbackIn) -> dict:
    """The user correcting the ranking. Cheapest, truest signal we get, so it
    outranks everything inferred -- and re-scoring costs no AI calls."""
    if body.verdict not in db.VALID_VERDICTS:
        raise HTTPException(status_code=400, detail=f"verdict must be one of {sorted(db.VALID_VERDICTS)}")
    until = None
    if body.verdict == "snoozed":
        hours = body.snooze_hours or 24
        until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    db.set_feedback(email_id, body.verdict, until)
    pipeline.rescore_all()
    return {"email_id": email_id, "verdict": body.verdict, "snooze_until": until}


class MuteIn(BaseModel):
    address: str


@router.get("/senders/muted")
def list_muted() -> dict:
    return {"items": db.muted_sender_rows()}


@router.get("/senders/impact")
def mute_impact(address: str) -> dict:
    """How much mail a mute would actually silence. The confirmation dialog
    shows this, because "mute" reads as "this message" unless you are told it
    means every message from this address, past and future."""
    return {"address": address.lower(), "message_count": db.count_from_sender(address)}


class HighlightIn(BaseModel):
    address: str
    color: str


@router.get("/senders/highlighted")
def list_highlighted() -> dict:
    return {"items": db.highlighted_sender_rows(), "colors": list(db.HIGHLIGHT_COLORS)}


@router.post("/senders/highlight")
def highlight(body: HighlightIn) -> dict:
    address = (body.address or "").strip().lower()
    if not address:
        raise HTTPException(status_code=400, detail="An address is required.")
    try:
        db.highlight_sender(address, body.color)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Highlighting un-mutes, so the ranking that mute suppressed has to return.
    pipeline.rescore_all()
    return {"address": address, "color": body.color,
            "message_count": db.count_from_sender(address)}


@router.delete("/senders/highlight")
def unhighlight(address: str) -> dict:
    db.unhighlight_sender(address)
    return {"address": address.lower(), "color": None}


@router.post("/senders/mute")
def mute(body: MuteIn) -> dict:
    address = (body.address or "").strip().lower()
    if not address:
        raise HTTPException(status_code=400, detail="An address is required.")
    db.mute_sender(address)
    pipeline.rescore_all()
    return {"address": address, "muted": True, "message_count": db.count_from_sender(address)}


@router.delete("/senders/mute")
def unmute(address: str) -> dict:
    db.unmute_sender(address)
    pipeline.rescore_all()
    return {"address": address.lower(), "muted": False}


@router.delete("/{email_id}/feedback")
def clear_feedback(email_id: str) -> dict:
    db.clear_feedback(email_id)
    pipeline.rescore_all()
    return {"email_id": email_id, "verdict": None}


@router.get("/{email_id}")
def get_mail(email_id: str) -> dict:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT e.*, c.bucket, c.deadline, c.score, c.matched, c.rationale, c.source, c.model "
            "FROM emails e LEFT JOIN classifications c ON c.email_id = e.id WHERE e.id = ?",
            (email_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Email not found in the local cache.")
    item = dict(row)
    item["to_recipients"] = db.json_list(item.get("to_recipients"))
    item["cc_recipients"] = db.json_list(item.get("cc_recipients"))
    item["matched"] = [m for m in (item.get("matched") or "").split(",") if m]
    return item


@router.post("/sync")
async def sync(classify: bool = True) -> dict:
    source = get_source()
    state = source.status()
    started = db.now_iso()
    if not state.ready:
        db.record_sync("user", started, ok=False, error=state.detail or "source not ready")
        # 428 = "fix your configuration"; 401 = "go sign in". The UI routes on this.
        raise HTTPException(status_code=401 if state.needs_auth else 428, detail=state.detail)
    try:
        result = await source.sync()
    except SourceError as exc:
        db.record_sync("user", started, ok=False, error=str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Recorded on the way through, same as the poller's runs, so the history is
    # one history rather than two that disagree about what happened.
    db.record_sync("user", started, ok=True, scanned=result.get("scanned", 0),
                   fetched=result.get("fetched", 0), written=result.get("written", 0))
    result["freshness"] = db.source_freshness()
    if classify:
        result["classification"] = await pipeline.classify_pending()
    return result


def sync_state() -> dict:
    """What the UI needs to tell "nothing arrived" from "this is broken".

    Three different situations that used to render as the same empty-ish list:
      ok and moving        -- normal
      ok and frozen        -- the source itself has stopped (Mail.app closed?)
      failing              -- the sync is erroring, and has been for N attempts
    """
    last = db.last_sync()
    fresh = db.source_freshness()
    state = "unknown"
    if last is not None:
        if not last["ok"]:
            state = "failing"
        elif fresh["frozen_checks"] >= 3 and fresh["frozen_hours"] >= 12:
            # Three successful checks over half a day, all finding the same
            # newest message. That is not a quiet morning.
            state = "frozen"
        else:
            state = "ok"
    return {
        "state": state,
        "at": last["finished_at"] if last else None,
        "trigger": last["trigger"] if last else None,
        "error": last["error"] if last else "",
        "consecutive_failures": last["consecutive_failures"] if last else 0,
        **fresh,
    }


@router.post("/classify")
async def classify(limit: int = 100) -> dict:
    return await pipeline.classify_pending(limit)


@router.post("/rescan")
async def rescan() -> dict:
    """Re-read the whole cached mailbox, including for to-dos.

    Deliberately a user-pressed button: it re-runs the model over everything,
    which is the one genuinely expensive thing this app can do."""
    return await pipeline.rescan()


@router.get("/digest/today")
async def digest(force: bool = False) -> dict:
    return await pipeline.build_digest(force=force)
