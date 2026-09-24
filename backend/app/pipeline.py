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
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import db, dedup, learning, priority
from .llm.base import Classification, ProviderUnavailable
from .llm.registry import get_provider

# Set whenever an LLM call fails so the UI can show the "AI unavailable" badge
# with a real reason instead of a shrug.
_ai_status: dict[str, Any] = {"available": True, "off": False, "detail": "",
                              "detail_key": None, "detail_vars": {}}
_classify_lock = asyncio.Lock()


def ai_status() -> dict[str, Any]:
    return dict(_ai_status)


def _mark_ai(available: bool, detail: str = "", off: bool = False,
             key: str | None = None, vars: dict[str, Any] | None = None) -> None:
    """`off` means the user chose "None" in Settings -- that is a preference,
    not an outage, and the UI should not nag about it.

    `key`/`vars` carry a recognised failure to the screen in the reader's
    language; `detail` stays as the English fallback for anything unexpected.
    See ProviderUnavailable."""
    _ai_status["available"] = available
    _ai_status["off"] = off
    _ai_status["detail"] = detail
    _ai_status["detail_key"] = key
    _ai_status["detail_vars"] = vars or {}


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

def structural_reason_codes(
    email: dict[str, Any], bucket: str, deadline: str | None, me: str
) -> list[dict[str, Any]]:
    """The signals that actually fired, as translation keys.

    Keys, not a sentence. This used to return English prose -- "Deadline
    2026-09-18 found in the text; flagged high importance; has an attachment."
    -- which is display text, and display text built in the backend is display
    text in one language. It sat in the middle of a Korean reading pane for as
    long as the feature has existed.

    **Exactly the mistake already fixed once, in the briefing**, where the
    structural path now emits `note_key` for the same reason (see
    `_as_agenda_row`). The rule the second occurrence earns: the backend names
    *what it found*; the frontend decides what that is called.
    """
    out: list[dict[str, Any]] = []
    if deadline:
        out.append({"key": "sigDeadline", "vars": {"date": deadline}})
    if priority._addressed_directly(email, me):
        out.append({"key": "sigDirect"})
    elif priority._cc_only(email, me):
        out.append({"key": "sigCcOnly"})
    if (email.get("importance") or "").lower() == "high":
        out.append({"key": "sigHighImportance"})
    if bucket == "action":
        out.append({"key": "sigAsksAction"})
    elif bucket == "noise":
        out.append({"key": "sigBulk"})
    if email.get("has_attachments"):
        out.append({"key": "sigAttachment"})
    return out or [{"key": "sigNone"}]


def _structural_reason(email: dict[str, Any], bucket: str, deadline: str | None, me: str) -> str:
    """The same codes, as the JSON string the `rationale` column holds.

    One column carries two shapes, and that is deliberate rather than sloppy:
    the model path writes prose *in the user's language* (it was asked to), and
    only the structural path has anything to translate. The frontend tells them
    apart by trying to parse -- an array of `{key}` objects is codes, anything
    else is a finished sentence.
    """
    return json.dumps(structural_reason_codes(email, bucket, deadline, me))


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
            category=c.category,
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

        # The to-dos read out of the body are the real answer to "what do I owe
        # anyone?". Reconciled rather than inserted, so re-reading a message
        # neither duplicates its commitments nor resurrects ones already dealt
        # with. A structural pass carries no tasks and must not wipe the ones a
        # previous model pass found, so an empty list from the fallback is
        # skipped rather than synced.
        #
        # **A noise email contributes no to-dos at all**, and this runs after
        # the bucket is derived so it can know that. Extracting obligations from
        # mail the app has just decided is not about your life is incoherent,
        # and it was the mechanism behind the calendar filling up: one
        # departmental newsletter listing four programmes became four personal
        # commitments. Syncing to an empty list rather than skipping is what
        # clears the ones already there -- and `sync_email_tasks` deletes only
        # rows no other message names and the user never touched, so a to-do
        # that was ticked, removed, or typed by hand survives.
        if bucket == "noise":
            db.sync_email_tasks(c.email_id, [])
        elif c.tasks or source == "llm":
            db.sync_email_tasks(
                c.email_id,
                [{"title": t.title, "due_date": t.due_date} for t in c.tasks],
            )

        verdict = (feedback.get(c.email_id) or {}).get("verdict")
        score, matched = priority.score_email(
            email, bucket, deadline, priorities,
            llm_matched=c.matched, user_address=me, today=today,
            correspondents=correspondents,
            verdict=verdict,
            sender_muted=(email.get("from_address") or "").lower() in muted,
        )
        # Computed after scoring so it can name the topic that actually matched,
        # and stored beside the score rather than recomputed on read -- the list
        # must not need one API call per row to say why a row is there.
        reason_code, reason_arg = learning.reason_for_email(
            axes, matched or axis_topics, deadline, verdict=verdict, today=today,
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
            "reason_code": reason_code,
            "reason_arg": reason_arg,
            # Kept apart from `bucket` on purpose: `bucket` is derived and will
            # be re-derived, so overwriting the model's own answer with it would
            # destroy the only unrewritten input rescore_all has.
            "model_bucket": c.bucket,
            "category": c.category,
            "summary": c.summary,
        })

    # Exploration, decided over the whole suppressed batch. An oracle ranker
    # maximises feedback-loop degeneracy, and `noise` is otherwise a one-way
    # door: a muted sender can never be discovered to matter again, because
    # nothing they send is ever shown to be corrected.
    chosen = learning.explored_in_batch(explorable)
    if chosen:
        with db.connect() as conn:
            # "Shown as a guess" outranks every inferred reason -- it IS the
            # reason the row is on screen. A pin is the user's own word and
            # keeps precedence over both.
            conn.executemany(
                "UPDATE classifications SET explored = 1, "
                "reason_code = CASE WHEN reason_code = 'pinned' THEN reason_code "
                "ELSE 'explored' END WHERE email_id = ?",
                [(i,) for i in chosen],
            )
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
            _mark_ai(False, "" if turned_off else str(exc), off=turned_off,
                     key=None if turned_off else getattr(exc, "key", None),
                     vars=getattr(exc, "vars", None))
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
                _mark_ai(False, detail, key=getattr(exc, "key", None),
                         vars=getattr(exc, "vars", None))
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
        _mark_ai(False, str(exc), key=getattr(exc, "key", None),
                 vars=getattr(exc, "vars", None))
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
            "SELECT e.*, c.bucket, c.deadline, c.matched, c.model_bucket, c.category "
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
                category=email.get("category"),
                user_address=me,
                correspondents=correspondents,
                highlights=highlights,
                weights=weights,
                centroids=centroids,
            )
            bucket = axes.bucket(thresholds)
            if bucket == "noise" and (email.get("from_address") or "").lower() not in muted:
                explorable.append(email["id"])

            verdict = (feedback.get(email["id"]) or {}).get("verdict")
            score, matched = priority.score_email(
                email, bucket, email.get("deadline"),
                priorities, llm_matched=llm_matched, user_address=me, today=today,
                correspondents=correspondents,
                verdict=verdict,
                sender_muted=(email.get("from_address") or "").lower() in muted,
            )
            reason_code, reason_arg = learning.reason_for_email(
                axes, matched or axis_topics, email.get("deadline"),
                verdict=verdict, today=today,
            )
            conn.execute(
                "UPDATE classifications SET score = ?, matched = ?, bucket = ?, "
                "actionability = ?, relevance = ?, explored = 0, "
                "reason_code = ?, reason_arg = ? WHERE email_id = ?",
                (score, ",".join(matched or axis_topics), bucket,
                 axes.actionability, axes.relevance, reason_code, reason_arg,
                 email["id"]),
            )
            updated += 1
        conn.commit()

    chosen = learning.explored_in_batch(explorable)
    if chosen:
        with db.connect() as conn:
            conn.executemany(
                "UPDATE classifications SET explored = 1, "
                "reason_code = CASE WHEN reason_code = 'pinned' THEN reason_code "
                "ELSE 'explored' END WHERE email_id = ?",
                [(i,) for i in chosen],
            )
            conn.commit()
    return updated


# --------------------------------------------------------------------------
# digest
# --------------------------------------------------------------------------

# How far past due a thing can be and still belong in "today". Two days,
# because the deadline curve already treats an overdue-by-two as very likely
# still actionable -- past that it is a thing you missed, not a thing today.
DIGEST_OVERDUE_GRACE = 2


def _digest_rank(deadline: str | None, score: float, today: date) -> tuple[int, float, float]:
    """Sort key for the briefing: when it is due first, how it ranks second.

    The briefing answers "what should I do today", and the score alone does not
    answer that. Score mixes urgency with relevance, so a strongly-relevant
    email whose deadline passed last week outranks a moderately-relevant one due
    this afternoon -- which is how a list headed "What's crucial today" filled
    with things that were crucial last Tuesday.

    Three tiers, then score inside each:

      0  due today or within the look-ahead window   <- what today is for
      1  overdue by no more than the grace period    <- still catchable
      2  everything else, dated or not

    Deliberately an ordering and not a filter. When nothing at all is due this
    week the briefing should still say something useful rather than go blank,
    and a two-day-overdue item is exactly what it should say.
    """
    if not deadline:
        return (2, 0.0, -score)
    try:
        days = (date.fromisoformat(deadline) - today).days
    except ValueError:
        return (2, 0.0, -score)
    horizon = priority.horizon_days()
    if 0 <= days <= horizon:
        # Nearest first inside the window: today beats Friday.
        return (0, float(days), -score)
    if -DIGEST_OVERDUE_GRACE <= days < 0:
        # Most recently missed first -- yesterday is likelier to be live.
        return (1, float(-days), -score)
    return (2, 0.0, -score)


def _digest_candidates(limit: int = 25, today: date | None = None) -> list[dict[str, Any]]:
    """Mail that still needs the user, ordered by when it is due.

    Anything already ticked off, muted or snoozed is excluded -- a briefing
    that lists things you have already dealt with trains you to ignore the
    briefing.
    """
    today = today or date.today()
    # No verdict filter in the SQL any more. The collapser has to see the
    # decided copies too, or ticking the row it shows promotes a sibling the
    # user never saw and the announcement comes straight back wearing a
    # different id. Eligibility is applied below instead.
    where = ("WHERE c.bucket != 'noise' "
             "  AND e.from_address NOT IN (SELECT address FROM muted_senders) ")
    join = ("FROM emails e JOIN classifications c ON c.email_id = e.id "
            "LEFT JOIN feedback f ON f.email_id = e.id ")

    # Two passes, and the reason is not performance alone.
    #
    # Selecting the top N by score and then re-sorting them by date decides the
    # shortlist on the wrong axis: an email due tomorrow that sits below the cut
    # never reaches the sort meant to promote it, so the briefing would still
    # lead with whatever was stale and loud. Over-fetching by some factor only
    # moves where that cliff sits.
    #
    # So: rank EVERY candidate on ids and dates alone -- three small columns,
    # no bodies -- and only then fetch the full rows for the handful that won.
    with db.connect() as conn:
        keys = conn.execute(
            "SELECT e.id, e.conversation_id, e.subject, e.from_address, e.received_at, "
            f"       c.deadline, c.score, f.verdict {join}{where}").fetchall()

    # One announcement, one candidate, before anything is ranked.
    #
    # Here rather than after the cut, because a department that sends the same
    # notice three times would otherwise spend three of seven briefing slots on
    # it -- and the top-up would keep the count at seven by pulling in yet more
    # of the same. Measured on the real mailbox: 1,143 candidates, 837
    # announcements.
    keys = dedup.collapse(
        [dict(r) for r in keys],
        eligible=lambda r: (r["verdict"] or "") in ("", "pinned"),
        drop_groups=lambda r: (r["verdict"] or "") in _SETTLED_VERDICTS,
    )
    copies = {r["id"]: r.get("copies", 1) for r in keys}

    with db.connect() as conn:

        # Materialised, not looked up inside the sort key: a `next(... for k in
        # keys ...)` there is a linear scan per comparison, which on a
        # thousand-row mailbox is a million string compares to choose seven
        # lines of a briefing.
        scored = [(r["id"],
                   None if priority.is_forgotten(r["deadline"], today) else r["deadline"],
                   r["score"] or 0.0)
                  for r in keys]
        ranked = [(eid, deadline) for eid, deadline, _ in sorted(
            scored, key=lambda row: _digest_rank(row[1], row[2], today))[:limit]]
        if not ranked:
            return []

        order = {eid: i for i, (eid, _) in enumerate(ranked)}
        forgotten = {eid for eid, deadline in ranked if deadline is None}
        placeholders = ",".join("?" for _ in order)
        rows = conn.execute(
            "SELECT e.id, e.subject, e.from_name, e.from_address, e.received_at, "
            f"       e.body_text, e.body_preview, c.bucket, c.deadline, c.score {join}"
            f"WHERE e.id IN ({placeholders})", tuple(order)).fetchall()

    out = []
    for row in rows:
        email = dict(row)
        # A deadline a month past is not a deadline any more. Blanked here
        # rather than filtered, because the mail may still be worth raising for
        # other reasons -- it just must not be introduced as something due.
        if email["id"] in forgotten:
            email["deadline"] = None
        email["copies"] = copies.get(email["id"], 1)
        out.append(email)
    out.sort(key=lambda e: order[e["id"]])
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
        # How many messages this row stands for. 1 unless the announcement was
        # sent more than once -- shown as a count so a collapsed group reads as
        # "several copies" rather than as mail that went missing.
        "copies": email.get("copies", 1),
    }


# How many OPEN items the briefing aims to show.
#
# It was a literal 7 inside `_fallback_agenda`, which made it the size of a
# list built once a day rather than a target maintained through the day -- so
# clearing three things left four, and the queue behind them was not consulted
# again until tomorrow. With a backlog that also means the briefing only ever
# reaches a day or two out, because the seven nearest deadlines are all it can
# ever contain.
DIGEST_TARGET_ITEMS = 7


# Bump whenever the rules behind a briefing change. The digest is cached for a
# calendar day, so without this a fix to what belongs in it is invisible until
# tomorrow -- which is exactly how a briefing kept announcing a deadline from
# 2022 hours after the code that produced it had been replaced.
DIGEST_LOGIC_VERSION = 3


# A verdict that takes the row out of the briefing. `pinned` does not: pinning
# says "this matters", which is the opposite of "I am finished with it".
_SETTLED_VERDICTS = ("done", "not_relevant", "snoozed")

# How long a briefing that fell back to structural (AI configured but not
# answering) is served before the model is tried again.
DIGEST_AI_RETRY_MINUTES = 10


def _with_current_verdicts(digest: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Join on what the user has since done, drop what they have finished, and
    refill from the queue behind it.

    Three things happen here rather than at build time, and all three for the
    same reason: **the briefing is cached for a calendar day, and the mailbox
    is not.** A list frozen at 08:00 and served unchanged until midnight can
    only ever shrink.

    1. *Verdicts are joined on.* The tick used to be derived in the frontend by
       looking the row's id up in the mail list -- two different queries, so a
       row the list did not happen to contain could never show as ticked.
       Clicking it posted the feedback and nothing visibly happened, which
       reads as a dead checkbox.

    2. *Settled rows leave.* Ticked, dismissed and snoozed mail is finished
       with; keeping it on the checklist is how a checklist stops being one.
       The undo toast, not the row, is what makes a mis-click recoverable, and
       it is already there for six seconds after every gesture.

    3. *The gap is refilled* from the same ranked queue the briefing was built
       from. Without this, clearing four of seven items left three, until
       tomorrow -- and since the seven nearest deadlines are all seven slots
       can hold, the briefing never showed anything more than a day or two out
       no matter how much of it you cleared.

    Refills are structural rows even when the briefing itself came from a
    model: a model call per tick is not worth it, and `_structural_row` is what
    the no-AI path already uses, so a replacement is described exactly like
    what it replaced.
    """
    items = digest.get("items") or []
    feedback = db.all_feedback()
    for item in items:
        item["verdict"] = (feedback.get(item.get("email_id")) or {}).get("verdict")

    kept = [i for i in items if (i.get("verdict") or "") not in _SETTLED_VERDICTS]
    missing = DIGEST_TARGET_ITEMS - len(kept)
    if missing > 0:
        today = today or date.today()
        seen = {i.get("email_id") for i in items}
        # Deeper than the gap: the candidate query cannot know which ids are
        # already on the list, so the shortlist has to survive skipping them.
        for email in _digest_candidates(limit=DIGEST_TARGET_ITEMS * 4, today=today):
            if missing <= 0:
                break
            if email["id"] in seen:
                continue
            row = _structural_row(email, today.isoformat())
            row["verdict"] = None
            kept.append(row)
            missing -= 1

    if len(kept) != len(items):
        digest["items"] = kept
        digest["headline"] = _restate_headline(digest.get("headline"), kept)
    else:
        digest["items"] = kept
    return digest


def _restate_headline(headline: Any, rows: list[dict[str, Any]]) -> Any:
    """Keep the count in the headline honest when the rows underneath change.

    Only the structural headline can be recomputed -- it is a key and two
    numbers. A model wrote its own sentence about the day and there is nothing
    here that can edit it truthfully, so it is left exactly as it was rather
    than being half-corrected.
    """
    if not isinstance(headline, dict) or "key" not in headline:
        return headline
    # The empty-state headlines are included on purpose. A briefing built when
    # the mailbox had nothing waiting, then topped up after a sync, would
    # otherwise announce "nothing waiting" above seven rows.
    if headline["key"] not in ("agendaHeadline", "agendaHeadlineNoDeadline",
                               "nothingWaiting", "nothingUrgent"):
        return headline
    if not rows:
        return headline
    urgent = sum(1 for r in rows if r.get("deadline"))
    return {
        "key": "agendaHeadline" if urgent else "agendaHeadlineNoDeadline",
        "vars": {"n": len(rows), "d": urgent},
    }


def cached_digest(day: str | None = None) -> dict[str, Any] | None:
    day = day or date.today().isoformat()
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM digests WHERE day = ?", (day,)).fetchone()
    if row is None:
        return None
    cached = _unpack(dict(row))
    if cached.get("logic_version") != DIGEST_LOGIC_VERSION:
        return None            # built by older rules; rebuild rather than serve

    # A briefing is stamped with what produced it, and the briefing is cached for
    # the whole day. Switch provider at lunchtime and the morning's stamp keeps
    # naming the old one -- the footer then reports a fact that stopped being
    # true, which is worse than reporting nothing. Rebuild instead.
    stamped = (cached.get("model") or "").split(":", 1)[0]
    configured = (db.get_setting("llm_provider", "none") or "none").lower()
    if stamped and stamped not in ("none", "structural") and stamped != configured:
        return None
    if stamped == "structural" and configured not in ("", "none"):
        # It fell back because AI was unavailable. If a provider has since been
        # configured, the fallback is not the answer any more -- but only try
        # again every few minutes. This check used to rebuild on EVERY read, so
        # each tick in the briefing (which re-reads it to drop the row and pull
        # up the next one) waited on a fresh model call that was going to fail
        # the same way; the ticked row sat there struck through until it did.
        built = cached.get("created_at") or ""
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(built)
        except ValueError:
            age = timedelta(days=1)
        if age >= timedelta(minutes=DIGEST_AI_RETRY_MINUTES):
            return None
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
            _mark_ai(False, "" if turned_off else str(exc), off=turned_off,
                     key=None if turned_off else getattr(exc, "key", None),
                     vars=getattr(exc, "vars", None))
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


def _structural_row(email: dict[str, Any], today: str) -> dict[str, Any]:
    """Say the truest thing about this row.

    Shared by the no-AI briefing and by the top-up, so a replacement row is
    described exactly like the row it replaced. A top-up row is always
    structural even when the briefing came from a model: filling a gap is not
    worth a model call per tick, and a row whose note is missing would look
    like a different kind of row.
    """
    deadline = email.get("deadline")
    if not deadline:
        return _as_agenda_row(email, note_key="needsReply")
    if deadline == today:
        return _as_agenda_row(email, note_key="dueToday")
    return _as_agenda_row(email, note_key="dueOn", note_vars={"date": deadline})


def _fallback_agenda(
    emails: list[dict[str, Any]], today: date | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """A useful checklist with no model at all: deadlines first, then actions.

    Every row still points at a real email, so the briefing stays clickable and
    tickable whether or not AI is available."""
    today = (today or date.today()).isoformat()

    # Previously only the first five dated emails got a date note and the rest
    # fell through to "needs a reply" -- so a message with a real deadline was
    # described as if it had none, while its chip still showed one. What a row
    # says follows from the row, not from where it landed in the list.
    def row_for(email: dict[str, Any]) -> dict[str, Any]:
        return _structural_row(email, today)

    rows: list[dict[str, Any]] = []
    used: set[str] = set()

    # Ascending, so the most pressing comes first -- but only among dates that
    # still mean something. Without the filter in _digest_candidates the single
    # oldest deadline in the mailbox led the briefing every single morning.
    #
    # Ascending by date was still the wrong order once the filter existed: the
    # oldest *surviving* deadline led instead, which on a mailbox with a backlog
    # means the briefing opens with something that was due last week. Same rank
    # as the model path uses, so the no-AI briefing and the AI one agree about
    # what "today" means rather than differing by which of them is running.
    day = date.fromisoformat(today)
    dated = sorted((e for e in emails if e.get("deadline")),
                   key=lambda e: _digest_rank(e["deadline"], e.get("score") or 0.0, day))
    for e in dated[:5]:
        rows.append(row_for(e))
        used.add(e["id"])

    for e in emails:
        if len(rows) >= DIGEST_TARGET_ITEMS:
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
