"""The db-facing half of adaptive ranking.

`relevance.py` is pure -- it takes dictionaries and returns numbers, and every
rule in it can be tested without a database. This module is the part that
touches state: loading thresholds, offering signals to the model, recording
what was refused, and keeping topic centroids fed.

The split is deliberate. The interesting rules are the ones about *when not to
learn*, and a rule that can only be exercised through sqlite is a rule nobody
will write a test for.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Sequence

from . import db, priority, relevance
from .relevance import Axes, Outcome, Signal, Thresholds

EMBEDDER_NAME = "hashing"

# Which gesture in the UI means what, and how much it is trusted. Kept here
# rather than in the pure module because it is a fact about this product's
# buttons, not about relevance ranking in general.
VERDICT_SIGNALS: dict[str, tuple[str, float]] = {
    "pinned":        ("explicit_correction", 1.0),
    # "This is not about me." The only gesture in the app that produces a clean
    # negative, and the reason it had to be added rather than folded into the
    # ones above: implicit feedback is structurally positive-only. Reading,
    # replying and dwelling all say "yes"; nothing a user does passively says
    # "this kind of thing does not concern me", so no amount of watching can
    # learn it. Only an explicit act can.
    #
    # Worth recording the counter-evidence with it. Mozilla's RegretsReporter
    # study (22,722 participants, 568M videos) measured what fraction of
    # unwanted recommendations each control actually prevented: the
    # source-scoped control ("don't recommend this channel" -- our mute sender)
    # prevented 43%, while the content-scoped one ("not interested" -- this
    # gesture) prevented 11%. Four times worse. That is not an argument against
    # having it; it is an argument that it must carry real weight or it is
    # theatre, which is exactly what their participants reported experiencing.
    "not_relevant":  ("explicit_correction", 0.0),
    "not_important": ("explicit_correction", 0.0),
    "done":          ("replied", 0.0),
    "snoozed":       ("opened_dwelled", 0.0),
}


# --------------------------------------------------------------------------
# thresholds
# --------------------------------------------------------------------------

def load_thresholds() -> Thresholds:
    def _num(key: str, fallback: float) -> float:
        try:
            return float(db.get_setting(key, "") or fallback)
        except (TypeError, ValueError):
            return fallback
    return Thresholds(
        relevance=_num("theta_relevance", relevance.DEFAULT_THETA_REL),
        action=_num("theta_action", relevance.DEFAULT_THETA_ACT),
        moved_today=_num("theta_moved_today", 0.0),
        moved_on=db.get_setting("theta_moved_on", ""),
    ).clamped()


def save_thresholds(thresholds: Thresholds) -> None:
    thresholds = thresholds.clamped()
    db.set_setting("theta_relevance", f"{thresholds.relevance:.4f}")
    db.set_setting("theta_action", f"{thresholds.action:.4f}")
    db.set_setting("theta_moved_today", f"{thresholds.moved_today:.6f}")
    db.set_setting("theta_moved_on", thresholds.moved_on or "")


def set_action_volume(target_per_day: int, actionabilities: Sequence[float], days: int = 1) -> Thresholds:
    """The user-facing control: a number of items, not a probability.

    Google's finding is that per-user thresholds cut error as much as the
    entire personalised model did, and that users tune them by hand because
    nobody agrees on the cost of a false positive. This is that control.
    """
    current = load_thresholds()
    solved = relevance.threshold_for_volume(actionabilities, target_per_day, days)
    if solved is None:
        return current
    updated = Thresholds(current.relevance, solved, current.moved_today, current.moved_on).clamped()
    save_thresholds(updated)
    db.set_setting("target_action_volume", str(int(target_per_day)))
    return updated


def nudge_threshold(recent: Sequence[tuple[str, float]], today: date | None = None) -> tuple[Thresholds, str]:
    """The consistent-direction rule. Returns the reason too, always, because a
    threshold that refused to move is exactly as interesting as one that did."""
    updated, reason = relevance.adjust_threshold(load_thresholds(), recent, today=today)
    save_thresholds(updated)
    return updated, reason


# --------------------------------------------------------------------------
# learning
# --------------------------------------------------------------------------

def epoch_start() -> str:
    return db.get_setting("learning_epoch_start", "")


def begin_epoch(at: str | None = None) -> str:
    """Draw the line: everything before this is invisible to learning.

    Set by hand, never inferred. The app was clicked through for weeks to check
    that the buttons worked, and those rows are indistinguishable from
    preference -- so the default is that none of them count.
    """
    stamp = at or db.now_iso()
    db.set_setting("learning_epoch_start", stamp)
    return stamp


def learn(signals: Iterable[Signal], features_by_email: dict[str, dict[str, float]]) -> dict[str, Any]:
    """Offer a batch of signals to the relevance model.

    Bursts are detected across the whole batch first -- a burst is a property of
    a *sequence*, so a signal examined alone can never be recognised as part of
    one. Then each signal is offered individually and the outcome recorded,
    accepted or refused.
    """
    batch = relevance.detect_bursts(list(signals))
    weights = db.ranking_weights()
    # Per-coordinate step sizes. Read once for the batch and updated in memory
    # as it goes, so ten signals in one batch do not all act as if they were
    # the first one to touch a feature.
    counts = db.weight_counts()
    touched: set[str] = set()
    applied = 0
    refused: dict[str, int] = {}

    for signal in batch:
        features = features_by_email.get(signal.email_id)
        if features is None:
            db.record_learning_event(signal.email_id, signal.kind, signal.target,
                                     False, "no-features")
            refused["no-features"] = refused.get("no-features", 0) + 1
            continue
        outcome: Outcome = relevance.apply_signal(
            signal, features, weights, epoch_start=epoch_start(), counts=counts
        )
        db.record_learning_event(
            signal.email_id, signal.kind, signal.target, outcome.applied,
            outcome.reason, outcome.confidence, outcome.error, outcome.deltas,
        )
        if outcome.applied:
            applied += 1
            for name in outcome.deltas:
                counts[name] = counts.get(name, 0) + 1
                touched.add(name)
        else:
            refused[outcome.reason] = refused.get(outcome.reason, 0) + 1

    if applied:
        db.save_ranking_weights(weights, touched)
    return {"offered": len(batch), "applied": applied, "refused": refused, "weights": weights}


def signals_from_feedback(rows: Iterable[dict[str, Any]]) -> list[Signal]:
    """Turn stored feedback rows into typed signals.

    Rows whose verdict has no mapping are dropped here rather than defaulted to
    something plausible: inventing a target for a gesture nobody has decided the
    meaning of is how a model learns something its author never claimed.
    """
    out: list[Signal] = []
    for row in rows:
        mapped = VERDICT_SIGNALS.get((row.get("verdict") or "").strip().lower())
        if not mapped:
            continue
        kind, target = mapped
        out.append(Signal(
            email_id=row.get("email_id") or "",
            kind=row.get("provenance") or kind,
            target=target,
            at=row.get("created_at") or "",
            sender=(row.get("from_address") or "").lower(),
            dwell_ms=int(row.get("dwell_ms") or 0),
        ))
    return out


# --------------------------------------------------------------------------
# axes
# --------------------------------------------------------------------------

def axes_for_email(
    email: dict[str, Any],
    *,
    model_bucket: str | None = None,
    deadline: str | None = None,
    task_count: int = 0,
    priorities: list[dict[str, Any]] | None = None,
    llm_matched: Iterable[str] = (),
    category: str | None = None,
    user_address: str = "",
    correspondents: dict[str, int] | None = None,
    highlights: dict[str, str] | None = None,
    weights: dict[str, float] | None = None,
    centroids: dict[str, list[float]] | None = None,
) -> tuple[Axes, list[str]]:
    """Both axes for one email, plus the topics it matched."""
    priorities = priorities if priorities is not None else priority.active_priorities()
    keyword_hits = [p.get("topic") for p in priority.match_priorities(email, priorities) if p.get("topic")]

    # The model's opinion about which topics an email matched is only meaningful
    # for topics that still exist. `matched` is stored on the classification, so
    # without this filter a deleted priority kept ranking mail forever: the
    # keyword side correctly stopped matching, the stored side did not, and
    # `rescore_all` faithfully reproduced the old score.
    #
    # That is precisely the case this design exists to handle -- "a professor
    # matters less once the course ends" -- failing in the one place nobody
    # looks, because the number did not change.
    live = {(p.get("topic") or "").strip().lower() for p in priorities}
    keyword_hits += [str(t) for t in (llm_matched or ())
                     if str(t).strip() and str(t).strip().lower() in live]

    match, matched = relevance.topic_match(
        email, centroids if centroids is not None else db.priority_centroids(EMBEDDER_NAME),
        keyword_hits=keyword_hits,
    )
    address = (email.get("from_address") or "").lower()
    affinity = 0.0
    if correspondents and address:
        affinity = min(1.0, correspondents.get(address, 0) / 5.0)

    axes = relevance.axes_for(
        email,
        topic_match=match,
        model_bucket=model_bucket,
        deadline=deadline,
        task_count=task_count,
        addressed_directly=priority._addressed_directly(email, user_address),
        affinity=affinity,
        highlighted=bool(highlights and address in highlights),
        category=category,
        weights=weights if weights is not None else db.ranking_weights(),
    )
    return axes, matched


def reason_for_email(
    axes,
    matched_topics: Sequence[str],
    deadline: str | None,
    *,
    verdict: str | None = None,
    explored: bool = False,
    today: date | None = None,
) -> tuple[str, str]:
    """The stored one-line reason. Days-to-deadline is resolved here because the
    pure layer has no clock and should not acquire one."""
    days = None
    if deadline:
        try:
            days = (date.fromisoformat(str(deadline)[:10]) - (today or date.today())).days
        except (ValueError, TypeError):
            days = None
        if days is not None and (days < -priority.FORGET_AFTER_DAYS or days > 14):
            # Outside the window the app is willing to talk about a date, the
            # date is not the reason for anything.
            days = None
    return relevance.reason_for(
        axes,
        matched_topics=matched_topics,
        deadline_days=days,
        pinned=(verdict == "pinned"),
        explored=explored,
    )


def explain_email(email_id: str) -> dict[str, Any] | None:
    """Why this email is where it is. Returns None when it has no classification
    yet, rather than a plausible-looking explanation of a score that was never
    computed."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT e.*, c.bucket, c.deadline, c.actionability, c.relevance, c.explored "
            "FROM emails e JOIN classifications c ON c.email_id = e.id WHERE e.id = ?",
            (email_id,),
        ).fetchone()
    if row is None:
        return None
    email = dict(row)
    axes, matched = axes_for_email(
        email,
        model_bucket=email.get("bucket"),
        deadline=email.get("deadline"),
        user_address=db.get_setting("user_address", ""),
        highlights=db.sender_highlights(),
    )
    out = relevance.explain(axes, load_thresholds(), db.ranking_weights())
    out["email_id"] = email_id
    out["matched_topics"] = matched
    out["explored"] = bool(email.get("explored"))
    out["stored_bucket"] = email.get("bucket")
    return out


# --------------------------------------------------------------------------
# centroids
# --------------------------------------------------------------------------

def seed_centroids(priorities: list[dict[str, Any]] | None = None) -> int:
    """Give every active topic a centroid from its own name.

    A topic starts as the words the user typed, which is the whole point of
    cold start: day one is their declaration and nothing the system inferred.
    """
    priorities = priorities if priorities is not None else priority.active_priorities()
    existing = db.priority_centroids(EMBEDDER_NAME)
    seeded = 0
    for prio in priorities:
        topic = (prio.get("topic") or "").strip()
        if not topic or topic in existing:
            continue
        db.save_priority_centroid(topic, relevance.embed(topic), EMBEDDER_NAME)
        seeded += 1
    return seeded


def drift_centroid(topic: str, engaged_texts: Sequence[str] = (), rejected_texts: Sequence[str] = ()) -> bool:
    """One Rocchio step for one topic. False when there is nothing to learn from.

    Returning False rather than writing an unchanged vector keeps `updated_at`
    honest -- a timestamp that moves when nothing moved is a lie the next
    person has to disprove.
    """
    topic = (topic or "").strip()
    if not topic or (not engaged_texts and not rejected_texts):
        return False
    centroids = db.priority_centroids(EMBEDDER_NAME)
    current = centroids.get(topic) or relevance.embed(topic)
    moved = relevance.rocchio(
        current,
        [relevance.embed(t) for t in engaged_texts],
        [relevance.embed(t) for t in rejected_texts],
    )
    if not any(moved):
        return False
    db.save_priority_centroid(topic, moved, EMBEDDER_NAME)
    return True


# --------------------------------------------------------------------------
# exploration
# --------------------------------------------------------------------------

def explore_one_in() -> int:
    try:
        return max(0, int(db.get_setting("explore_one_in", "20") or 20))
    except (TypeError, ValueError):
        return 20


def explored_in_batch(email_ids: Sequence[str]) -> set[str]:
    """Which of these suppressed messages to surface anyway, labelled as a guess.

    Takes the whole suppressed set rather than one id, because the promise is a
    proportion of that set -- and a per-email coin flip can deliver zero of them.
    Callers pass only mail that is both `noise` and not from a muted sender:
    mute is the user's standing decision and exploration is about the system's
    own guesses.
    """
    one_in = explore_one_in()
    if one_in <= 0:
        return set()
    return relevance.select_explored(email_ids, one_in, f"{relevance.EXPLORE_SALT}:noise")


def should_explore(email_id: str, bucket: str) -> bool:
    """Single-email convenience, for a message being reclassified on its own.

    Degenerate by construction: a population of one is either explored or not,
    so this cannot honour the proportion. `explored_in_batch` is the real entry
    point and the pipeline uses that.
    """
    if bucket != "noise":
        return False
    return email_id in explored_in_batch([email_id])
