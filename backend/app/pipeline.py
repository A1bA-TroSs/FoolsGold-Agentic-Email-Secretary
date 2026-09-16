"""Orchestration: classify new mail, score everything, produce the daily digest.

Two rules shape this file:
  1. Never block the UI on the model. Every LLM path has a structural fallback
     and reports which one it used, so the dashboard always renders.
  2. Never pay twice. Classification is cached per email id; scoring is cheap
     and is recomputed whenever priorities change.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

from . import db, learning, priority
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


def _ranking_context() -> tuple[dict[str, int], dict[str, dict[str, Any]], set[str]]:
    """The two whole-mailbox inputs the scorer needs. Fetched once per pass
    rather than per email -- correspondent affinity walks the Sent mailbox."""
    try:
        from .sources.registry import get_source
        correspondents = get_source().correspondents()
    except Exception:  # noqa: BLE001 - ranking must survive a source hiccup
        correspondents = {}
    return correspondents, db.all_feedback(), db.muted_senders()


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


def _soonest_obligation(c: Classification) -> str | None:
    """What this email actually costs the user next.

    A Co-op reminder can name a presentation in September and a progress report
    due this Saturday. Ranking on whichever date the model happened to put in
    `deadline` buries the one that is about to bite, so the email is scored on
    the earliest thing it asks of you -- which is also the date the user would
    name if you asked them why the mail matters."""
    candidates = [t.due_date for t in c.tasks if t.due_date]
    if c.deadline:
        candidates.append(c.deadline)
    return min(candidates) if candidates else None


def _persist(classifications: list[Classification], emails_by_id: dict[str, dict], source: str, model: str) -> None:
    priorities = priority.active_priorities()
    me = user_address()
    today = date.today()
    correspondents, feedback, muted = _ranking_context()
    # Loaded once for the whole batch. These are per-user state, not per-email,
    # and re-reading them inside the loop was the easy way to make classifying
    # 200 emails do 800 queries.
    thresholds = learning.load_thresholds()
    weights = db.ranking_weights()
    centroids = db.priority_centroids(learning.EMBEDDER_NAME)
    highlights = db.sender_highlights()
    explorable: list[str] = []
    for c in classifications:
        email = emails_by_id.get(c.email_id)
        if email is None:
            continue

        # The to-dos read out of the body are the real answer to "what do I owe
        # anyone?". Reconciled rather than inserted, so re-reading a message
        # neither duplicates its commitments nor resurrects ones already dealt
        # with. A structural pass carries no tasks and must not wipe the ones a
        # previous model pass found, so an empty list from the fallback is
        # skipped rather than synced.
        if c.tasks or source == "llm":
            db.sync_email_tasks(
                c.email_id,
                [{"title": t.title, "due_date": t.due_date} for t in c.tasks],
            )

        deadline = _soonest_obligation(c)

        # The bucket stops being the model's verdict and becomes derived from
        # two axes it does not both own. `c.bucket` survives as a *hint about
        # the text* feeding actionability; relevance is the user's half, and it
        # is the half that moves. Deriving rather than accepting is what gives
        # the fyi/action boundary a knob at all -- a category has none.
        axes, axis_topics = learning.axes_for_email(
            email,
            model_bucket=c.bucket,
            deadline=deadline,
            task_count=len(c.tasks),
            priorities=priorities,
            llm_matched=c.matched,
            user_address=me,
            correspondents=correspondents,
            highlights=highlights,
            weights=weights,
            centroids=centroids,
        )
        bucket = axes.bucket(thresholds)

        # A muted sender is a standing decision, and exploration must not
        # quietly overturn it -- "I do not want to see this" is a promise, not
        # a probability. Everything else in `noise` is a guess, and a fraction
        # of the guesses are surfaced labelled so the one-way door has a gap.
        # The selection itself happens after the loop, over the whole suppressed
        # set at once: a proportion cannot be honoured one email at a time.
        if bucket == "noise" and (email.get("from_address") or "").lower() not in muted:
            explorable.append(c.email_id)

        score, matched = priority.score_email(
            email, bucket, deadline, priorities,
            llm_matched=c.matched, user_address=me, today=today,
            correspondents=correspondents,
            verdict=(feedback.get(c.email_id) or {}).get("verdict"),
            sender_muted=(email.get("from_address") or "").lower() in muted,
        )
        db.save_classification({
            "email_id": c.email_id,
            "bucket": bucket,
            "deadline": deadline,
            "rationale": c.rationale,
            "score": score,
            "matched": ",".join(matched or axis_topics),
            "model": model,
            "source": source,
            "created_at": db.now_iso(),
            "actionability": axes.actionability,
            "relevance": axes.relevance,
            "explored": 0,
            # Kept apart from `bucket` on purpose: `bucket` is derived and will
            # be re-derived, so overwriting the model's own answer with it would
            # destroy the only unrewritten input rescore_all has.
            "model_bucket": c.bucket,
        })

    # Exploration, decided over the whole suppressed batch. An oracle ranker
    # maximises feedback-loop degeneracy, and `noise` is otherwise a one-way
    # door: a muted sender can never be discovered to matter again, because
    # nothing they send is ever shown to be corrected.
    for email_id in learning.explored_in_batch(explorable):
        with db.connect() as conn:
            conn.execute("UPDATE classifications SET explored = 1 WHERE email_id = ?",
                         (email_id,))
            conn.commit()


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

        # Mail the model never got to see is upgraded a batch at a time once it
        # is reachable again. Without this, one rate-limited first sync left an
        # inbox permanently ranked on structure alone: classification is cached
        # per email and never revisited, so nothing short of a full rescan
        # recovered -- and nothing told the user that.
        upgraded = 0
        if not used_fallback and done < limit:
            upgraded = await _upgrade_structural(provider, me, topics, batch_size,
                                                 budget=limit - done)

        return {
            "classified": done,
            "upgraded": upgraded,
            "source": "structural" if used_fallback else "llm",
            "ai_available": _ai_status["available"],
            "detail": detail,
        }


async def _upgrade_structural(provider, me: str, topics: list[str],
                              batch_size: int, budget: int) -> int:
    """Re-ask the model about mail that only has a structural verdict.

    Bounded by the same limit as a normal pass, so a recovering key costs one
    ordinary sync's worth of requests and no more."""
    ids = db.structural_email_ids(min(budget, batch_size))
    if not ids:
        return 0
    emails = db.get_emails(ids)
    if not emails:
        return 0
    by_id = {e["id"]: e for e in emails}
    try:
        results = await provider.classify_batch(emails, topics)
    except Exception as exc:  # noqa: BLE001 - the upgrade is best-effort
        _mark_ai(False, str(exc))
        return 0
    _persist(results, by_id, "llm", f"{provider.name}:{provider.model}")
    return len(results)


async def rescan(batch_limit: int = 100, max_passes: int = 40) -> dict[str, Any]:
    """Re-read every cached message from scratch.

    Classification is cached per email id and never revisited, which is right --
    it is the expensive half. But it means a mailbox classified before the
    to-do extractor existed will never grow to-dos on its own. This drops the
    cached verdicts and asks again.

    Costs one model round-trip per batch, so it is a button the user presses,
    never something that runs by itself. `max_passes` is a runaway guard: every
    pass writes a row for every email it was given (structurally, if the model
    fails), so the backlog strictly shrinks and the loop terminates on its own.
    """
    with db.connect() as conn:
        conn.execute("DELETE FROM classifications")
        conn.commit()

    total = 0
    source = "none"
    detail = ""
    for _ in range(max_passes):
        result = await classify_pending(limit=batch_limit)
        if not result.get("classified"):
            break
        total += result["classified"]
        source = result.get("source", source)
        detail = result.get("detail") or detail

    rescore_all()
    return {
        "classified": total,
        "source": source,
        "detail": detail,
        "ai_available": _ai_status["available"],
        "off": _ai_status["off"],
    }


def rescore_all() -> int:
    """Re-derive both axes and re-score every classified email, with no model call.

    This is what makes editing your priorities feel instant -- and it is now
    load-bearing for a second reason. Relevance is the volatile axis: it moves
    when the priority list changes, when a centroid drifts, and every time a
    signal updates a weight. If only the *score* were recomputed here, an email
    would keep whatever bucket it was given the day it arrived, and the entire
    adaptive layer would be invisible until the user pressed "Re-read
    everything" -- which calls the model, which is exactly what this function
    exists to avoid.

    So the bucket is derived again from the axes, using the stored model bucket
    as the actionability hint it always was. Actionability is stable, so
    re-deriving it costs nothing and changes nothing; relevance is not.
    """
    priorities = priority.active_priorities()
    me = user_address()
    today = date.today()
    correspondents, feedback, muted = _ranking_context()
    # Loaded once for the whole batch. These are per-user state, not per-email,
    # and re-reading them inside the loop was the easy way to make re-scoring
    # 200 emails do 800 queries.
    thresholds = learning.load_thresholds()
    weights = db.ranking_weights()
    centroids = db.priority_centroids(learning.EMBEDDER_NAME)
    highlights = db.sender_highlights()
    explorable: list[str] = []
    updated = 0
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT e.*, c.bucket, c.deadline, c.matched, c.model_bucket "
            "FROM emails e JOIN classifications c ON c.email_id = e.id"
        ).fetchall()
        for row in rows:
            email = dict(row)
            llm_matched = [m for m in (email.get("matched") or "").split(",") if m]

            # `model_bucket` is what the provider actually said. Falling back to
            # the derived `bucket` would feed this function its own previous
            # output, and a loop whose input is its last output drifts wherever
            # the first error pointed.
            hint = email.get("model_bucket") or email.get("bucket")
            axes, axis_topics = learning.axes_for_email(
                email,
                model_bucket=hint,
                deadline=email.get("deadline"),
                task_count=len(db.email_task_ids() & {email["id"]}),
                priorities=priorities,
                llm_matched=llm_matched,
                user_address=me,
                correspondents=correspondents,
                highlights=highlights,
                weights=weights,
                centroids=centroids,
            )
            bucket = axes.bucket(thresholds)
            if bucket == "noise" and (email.get("from_address") or "").lower() not in muted:
                explorable.append(email["id"])

            score, matched = priority.score_email(
                email, bucket, email.get("deadline"),
                priorities, llm_matched=llm_matched, user_address=me, today=today,
                correspondents=correspondents,
                verdict=(feedback.get(email["id"]) or {}).get("verdict"),
                sender_muted=(email.get("from_address") or "").lower() in muted,
            )
            conn.execute(
                "UPDATE classifications SET score = ?, matched = ?, bucket = ?, "
                "actionability = ?, relevance = ?, explored = 0 WHERE email_id = ?",
                (score, ",".join(matched or axis_topics), bucket,
                 axes.actionability, axes.relevance, email["id"]),
            )
            updated += 1
        conn.commit()

    chosen = learning.explored_in_batch(explorable)
    if chosen:
        with db.connect() as conn:
            conn.executemany(
                "UPDATE classifications SET explored = 1 WHERE email_id = ?",
                [(i,) for i in chosen],
            )
            conn.commit()
    return updated


# --------------------------------------------------------------------------
# digest
# --------------------------------------------------------------------------

def _digest_candidates(limit: int = 25, today: date | None = None) -> list[dict[str, Any]]:
    """Top-ranked mail that still needs the user. Anything already ticked off,
    muted or snoozed is excluded -- a briefing that lists things you have
    already dealt with trains you to ignore the briefing."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT e.id, e.subject, e.from_name, e.from_address, e.received_at, "
            "       e.body_text, e.body_preview, "
            "       c.bucket, c.deadline, c.score "
            "FROM emails e JOIN classifications c ON c.email_id = e.id "
            "LEFT JOIN feedback f ON f.email_id = e.id "
            "WHERE c.bucket != 'noise' "
            "  AND (f.verdict IS NULL OR f.verdict = 'pinned') "
            "  AND e.from_address NOT IN (SELECT address FROM muted_senders) "
            "ORDER BY c.score DESC LIMIT ?",
            (limit,),
        ).fetchall()

    today = today or date.today()
    out = []
    for row in rows:
        email = dict(row)
        # A deadline a month past is not a deadline any more. Blanked here
        # rather than filtered, because the mail may still be worth raising for
        # other reasons -- it just must not be introduced as something due.
        if priority.is_forgotten(email.get("deadline"), today):
            email["deadline"] = None
        out.append(email)
    return out


def _as_agenda_row(
    email: dict[str, Any],
    note: str = "",
    note_key: str | None = None,
    note_vars: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One briefing row.

    The structural path emits a translation *key* rather than a finished
    sentence, because the backend has no business generating display text --
    doing so left the briefing stubbornly English while the rest of the UI was
    in Korean. The LLM path still sends a literal note, since the model writes
    it in the user's language.
    """
    return {
        "email_id": email["id"],
        "subject": email.get("subject") or "(no subject)",
        "sender": email.get("from_name") or email.get("from_address") or "",
        "deadline": email.get("deadline"),
        "bucket": email.get("bucket") or "fyi",
        "note": note,
        "note_key": note_key,
        "note_vars": note_vars or {},
    }


# Bump whenever the rules behind a briefing change. The digest is cached for a
# calendar day, so without this a fix to what belongs in it is invisible until
# tomorrow -- which is exactly how a briefing kept announcing a deadline from
# 2022 hours after the code that produced it had been replaced.
DIGEST_LOGIC_VERSION = 2


def _with_current_verdicts(digest: dict[str, Any]) -> dict[str, Any]:
    """Stamp each briefing row with what the user has since done about it.

    The tick used to be derived in the frontend by looking the row's id up in
    the mail list -- two different queries, so any row the list did not happen
    to contain could never show as ticked. Clicking it posted the feedback and
    nothing visibly happened, which reads as a dead checkbox. The briefing is
    cached; the verdicts are not, so they are joined on at serve time and the
    row carries its own state."""
    items = digest.get("items") or []
    if not items:
        return digest
    feedback = db.all_feedback()
    for item in items:
        item["verdict"] = (feedback.get(item.get("email_id")) or {}).get("verdict")
    return digest


def cached_digest(day: str | None = None) -> dict[str, Any] | None:
    day = day or date.today().isoformat()
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM digests WHERE day = ?", (day,)).fetchone()
    if row is None:
        return None
    cached = _unpack(dict(row))
    if cached.get("logic_version") != DIGEST_LOGIC_VERSION:
        return None            # built by older rules; rebuild rather than serve
    return _with_current_verdicts(cached)


def _unpack(row: dict[str, Any]) -> dict[str, Any]:
    """The digest is stored as JSON in the body column. Older rows hold plain
    markdown, so fall back to treating the text as the headline."""
    body = row.get("body") or ""
    try:
        payload = json.loads(body)
        if isinstance(payload, dict):
            row["headline"] = payload.get("headline", "")
            row["items"] = payload.get("items", [])
            row["logic_version"] = payload.get("logic_version")
            return row
    except (ValueError, TypeError):
        pass
    row["headline"] = body
    row["items"] = []
    row["logic_version"] = None      # pre-versioning markdown row
    return row


async def build_digest(force: bool = False) -> dict[str, Any]:
    """Runs once per calendar day on first dashboard open, or on demand."""
    day = date.today().isoformat()
    if not force:
        cached = cached_digest(day)
        if cached:
            return cached

    emails = _digest_candidates()
    by_id = {e["id"]: e for e in emails}

    if not emails:
        headline: Any = {"key": "nothingWaiting"}
        items: list[dict[str, Any]] = []
        model = "none"
    else:
        try:
            provider = get_provider()
            headline, agenda = await provider.summarize(
                emails, priority.priority_topics(), db.get_setting("ui_language", "en")
            )
            items = [
                _as_agenda_row(by_id[a.email_id], a.note)
                for a in agenda if a.email_id in by_id
            ]
            model = f"{provider.name}:{provider.model}"
            _mark_ai(True, "")
            if not items:
                # The model answered but picked nothing usable; a briefing with
                # no rows is not a briefing.
                headline, items = _fallback_agenda(emails)
                model = "structural"
        except Exception as exc:  # noqa: BLE001
            turned_off = (db.get_setting("llm_provider", "copilot") or "").lower() == "none"
            _mark_ai(False, "" if turned_off else str(exc), off=turned_off)
            headline, items = _fallback_agenda(emails)
            model = "structural"

    payload = json.dumps({
        "headline": headline, "items": items, "logic_version": DIGEST_LOGIC_VERSION,
    })
    created = db.now_iso()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO digests (day, body, model, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET body=excluded.body, model=excluded.model, "
            "created_at=excluded.created_at",
            (day, payload, model, created),
        )
        conn.commit()
    return _with_current_verdicts({
        "day": day, "headline": headline, "items": items, "model": model,
        "created_at": created, "logic_version": DIGEST_LOGIC_VERSION,
    })


def _fallback_agenda(
    emails: list[dict[str, Any]], today: date | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """A useful checklist with no model at all: deadlines first, then actions.

    Every row still points at a real email, so the briefing stays clickable and
    tickable whether or not AI is available."""
    today = (today or date.today()).isoformat()

    def row_for(email: dict[str, Any]) -> dict[str, Any]:
        """Say the truest thing about this row.

        Previously only the first five dated emails got a date note and the
        rest fell through to "needs a reply" -- so a message with a real
        deadline was described as if it had none, while its chip still showed
        one. What a row says now follows from the row, not from where it
        happened to land in the list."""
        deadline = email.get("deadline")
        if not deadline:
            return _as_agenda_row(email, note_key="needsReply")
        if deadline == today:
            return _as_agenda_row(email, note_key="dueToday")
        return _as_agenda_row(email, note_key="dueOn", note_vars={"date": deadline})

    rows: list[dict[str, Any]] = []
    used: set[str] = set()

    # Ascending, so the most pressing comes first -- but only among dates that
    # still mean something. Without the filter in _digest_candidates the single
    # oldest deadline in the mailbox led the briefing every single morning.
    dated = sorted((e for e in emails if e.get("deadline")), key=lambda e: e["deadline"])
    for e in dated[:5]:
        rows.append(row_for(e))
        used.add(e["id"])

    for e in emails:
        if len(rows) >= 7:
            break
        if e["id"] in used or e.get("bucket") != "action":
            continue
        rows.append(row_for(e))
        used.add(e["id"])

    if not rows:
        return {"key": "nothingUrgent"}, []

    urgent = sum(1 for r in rows if r["deadline"])
    headline = {
        "key": "agendaHeadline" if urgent else "agendaHeadlineNoDeadline",
        "vars": {"n": len(rows), "d": urgent},
    }
    return headline, rows
