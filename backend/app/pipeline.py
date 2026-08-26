"""Orchestration: classify new mail, score everything, produce the daily digest.

Two rules shape this file:
  1. Never block the UI on the model. Every LLM path has a structural fallback
     and reports which one it used, so the dashboard always renders.
  2. Never pay twice. Classification is cached per email id; scoring is cheap
     and is recomputed whenever priorities change.
"""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from . import db, priority
from .llm.base import Classification, ProviderUnavailable
from .llm.registry import get_provider

# Set whenever an LLM call fails so the UI can show the "AI unavailable" badge
# with a real reason instead of a shrug.
_ai_status: dict[str, Any] = {"available": True, "off": False, "detail": ""}
_classify_lock = asyncio.Lock()


def ai_status() -> dict[str, Any]:
    return dict(_ai_status)


def _mark_ai(available: bool, detail: str = "", off: bool = False) -> None:
    """`off` means the user chose "None" in Settings -- that is a preference,
    not an outage, and the UI should not nag about it."""
    _ai_status["available"] = available
    _ai_status["off"] = off
    _ai_status["detail"] = detail


def _ranking_context() -> tuple[dict[str, int], dict[str, dict[str, Any]]]:
    """The two whole-mailbox inputs the scorer needs. Fetched once per pass
    rather than per email -- correspondent affinity walks the Sent mailbox."""
    try:
        from .sources.registry import get_source
        correspondents = get_source().correspondents()
    except Exception:  # noqa: BLE001 - ranking must survive a source hiccup
        correspondents = {}
    return correspondents, db.all_feedback()


def user_address() -> str:
    """Whatever the user told us, else whatever Outlook signed in as. Needed by
    every source, so it cannot live in the Graph tables alone."""
    explicit = db.get_setting("user_address", "").strip()
    if explicit:
        return explicit.lower()
    with db.connect() as conn:
        row = conn.execute("SELECT account FROM oauth_tokens WHERE id = 1").fetchone()
    return ((row["account"] if row else "") or "").lower()


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

def _structural_reason(email: dict[str, Any], bucket: str, deadline: str | None, me: str) -> str:
    """Name the signals that actually fired, so the reason line teaches the user
    how the ranking works instead of just saying 'no AI'."""
    reasons: list[str] = []
    if deadline:
        reasons.append(f"deadline {deadline} found in the text")
    if priority._addressed_directly(email, me):
        reasons.append("addressed directly to you")
    elif priority._cc_only(email, me):
        reasons.append("you are only on Cc")
    if (email.get("importance") or "").lower() == "high":
        reasons.append("flagged high importance")
    if bucket == "action":
        reasons.append("wording asks you to act")
    elif bucket == "noise":
        reasons.append("looks like a bulk or automated sender")
    if email.get("has_attachments"):
        reasons.append("has an attachment")
    return "; ".join(reasons).capitalize() + "." if reasons else "No strong signals either way."


def _structural_classifications(emails: list[dict[str, Any]], me: str) -> list[Classification]:
    out = []
    for email in emails:
        bucket = priority.structural_bucket(email, me)
        text = f"{email.get('subject','')}\n{email.get('body_text') or email.get('body_preview') or ''}"
        deadline = priority.extract_deadline(text)
        out.append(
            Classification(
                email_id=email["id"],
                bucket=bucket,
                deadline=deadline,
                rationale=_structural_reason(email, bucket, deadline, me),
            )
        )
    return out


def _persist(classifications: list[Classification], emails_by_id: dict[str, dict], source: str, model: str) -> None:
    priorities = priority.active_priorities()
    me = user_address()
    today = date.today()
    correspondents, feedback = _ranking_context()
    for c in classifications:
        email = emails_by_id.get(c.email_id)
        if email is None:
            continue
        score, matched = priority.score_email(
            email, c.bucket, c.deadline, priorities,
            llm_matched=c.matched, user_address=me, today=today,
            correspondents=correspondents,
            verdict=(feedback.get(c.email_id) or {}).get("verdict"),
        )
        db.save_classification({
            "email_id": c.email_id,
            "bucket": c.bucket,
            "deadline": c.deadline,
            "rationale": c.rationale,
            "score": score,
            "matched": ",".join(matched),
            "model": model,
            "source": source,
            "created_at": db.now_iso(),
        })


async def classify_pending(limit: int = 100) -> dict[str, Any]:
    """Classify every cached email that has no classification yet.

    Batched, because Copilot bills per request rather than per token -- 100 new
    emails should cost ~10 requests, not 100.
    """
    async with _classify_lock:
        ids = db.unclassified_email_ids(limit)
        if not ids:
            return {"classified": 0, "source": "none", "ai_available": _ai_status["available"]}

        emails = db.get_emails(ids)
        emails.sort(key=lambda e: e.get("received_at") or "", reverse=True)
        emails_by_id = {e["id"]: e for e in emails}
        me = user_address()
        topics = priority.priority_topics()
        batch_size = max(1, int(db.get_setting("classify_batch_size", "10") or 10))

        try:
            provider = get_provider()
        except ProviderUnavailable as exc:
            turned_off = (db.get_setting("llm_provider", "copilot") or "").lower() == "none"
            _mark_ai(False, "" if turned_off else str(exc), off=turned_off)
            _persist(_structural_classifications(emails, me), emails_by_id, "structural", "none")
            return {"classified": len(emails), "source": "structural", "ai_available": False,
                    "off": turned_off, "detail": "" if turned_off else str(exc)}

        done = 0
        used_fallback = False
        detail = ""
        for start in range(0, len(emails), batch_size):
            batch = emails[start : start + batch_size]
            try:
                results = await provider.classify_batch(batch, topics)
                _persist(results, emails_by_id, "llm", f"{provider.name}:{provider.model}")
                done += len(results)
                # Anything the model skipped still needs a row, or we re-ask forever.
                answered = {r.email_id for r in results}
                missing = [e for e in batch if e["id"] not in answered]
                if missing:
                    _persist(_structural_classifications(missing, me), emails_by_id, "structural", "none")
                    done += len(missing)
                _mark_ai(True, "")
            except Exception as exc:  # noqa: BLE001 - degrade, never crash the sync
                used_fallback = True
                detail = str(exc)
                _mark_ai(False, detail)
                _persist(_structural_classifications(batch, me), emails_by_id, "structural", "none")
                done += len(batch)

        return {
            "classified": done,
            "source": "structural" if used_fallback else "llm",
            "ai_available": _ai_status["available"],
            "detail": detail,
        }


def rescore_all() -> int:
    """Re-run scoring against the current priority list without re-calling the
    model. This is what makes editing your priorities feel instant."""
    priorities = priority.active_priorities()
    me = user_address()
    today = date.today()
    correspondents, feedback = _ranking_context()
    updated = 0
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT e.*, c.bucket, c.deadline, c.matched FROM emails e "
            "JOIN classifications c ON c.email_id = e.id"
        ).fetchall()
        for row in rows:
            email = dict(row)
            llm_matched = [m for m in (email.get("matched") or "").split(",") if m]
            score, matched = priority.score_email(
                email, email.get("bucket") or "fyi", email.get("deadline"),
                priorities, llm_matched=llm_matched, user_address=me, today=today,
                correspondents=correspondents,
                verdict=(feedback.get(email["id"]) or {}).get("verdict"),
            )
            conn.execute(
                "UPDATE classifications SET score = ?, matched = ? WHERE email_id = ?",
                (score, ",".join(matched), email["id"]),
            )
            updated += 1
        conn.commit()
    return updated


# --------------------------------------------------------------------------
# digest
# --------------------------------------------------------------------------

def _digest_candidates(limit: int = 25) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT e.subject, e.from_name, e.from_address, e.body_text, e.body_preview, "
            "       c.bucket, c.deadline, c.score "
            "FROM emails e JOIN classifications c ON c.email_id = e.id "
            "WHERE c.bucket != 'noise' "
            "ORDER BY c.score DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def cached_digest(day: str | None = None) -> dict[str, Any] | None:
    day = day or date.today().isoformat()
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM digests WHERE day = ?", (day,)).fetchone()
    return dict(row) if row else None


async def build_digest(force: bool = False) -> dict[str, Any]:
    """Runs once per calendar day on first dashboard open, or on demand."""
    day = date.today().isoformat()
    if not force:
        cached = cached_digest(day)
        if cached:
            return cached

    emails = _digest_candidates()
    if not emails:
        body = "Nothing needing your attention has synced yet. Hit refresh to pull your inbox."
        model = "none"
    else:
        try:
            provider = get_provider()
            body = await provider.summarize(emails, priority.priority_topics())
            model = f"{provider.name}:{provider.model}"
            _mark_ai(True, "")
        except Exception as exc:  # noqa: BLE001
            turned_off = (db.get_setting("llm_provider", "copilot") or "").lower() == "none"
            _mark_ai(False, "" if turned_off else str(exc), off=turned_off)
            body = _fallback_digest(emails)
            model = "structural"

    with db.connect() as conn:
        conn.execute(
            "INSERT INTO digests (day, body, model, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET body=excluded.body, model=excluded.model, "
            "created_at=excluded.created_at",
            (day, body, model, db.now_iso()),
        )
        conn.commit()
    return {"day": day, "body": body, "model": model, "created_at": db.now_iso()}


def _fallback_digest(emails: list[dict[str, Any]]) -> str:
    """A useful digest with no model at all: deadlines first, then actions."""
    today = date.today().isoformat()
    lines: list[str] = []
    dated = [e for e in emails if e.get("deadline")]
    dated.sort(key=lambda e: e["deadline"])
    for e in dated[:5]:
        when = "today" if e["deadline"] == today else e["deadline"]
        lines.append(f"- **Due {when}** - {e['subject']} ({e.get('from_name') or e.get('from_address')})")
    actions = [e for e in emails if e.get("bucket") == "action" and not e.get("deadline")]
    for e in actions[:5]:
        lines.append(f"- Needs a reply - {e['subject']} ({e.get('from_name') or e.get('from_address')})")
    if not lines:
        return "Nothing looks time-critical today."
    # No "built without AI" preamble here -- the card already shows that in its footer.
    return "\n".join(lines)
