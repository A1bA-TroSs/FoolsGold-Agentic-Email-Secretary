"""The adaptive layer: two axes, two thresholds, and learning that can refuse.

Why this module exists
----------------------
`classify()` returned a *category*, and a category has no knob. If too much
lands in `action` there is nothing to turn -- the only available move is to
rewrite the prompt, which changes every email at once in unknown directions.
That is why the fyi/action boundary was never adjustable: the boundary was
never an object.

So the bucket stops being a model output and becomes *derived* from two
independent axes:

  actionability  A(e)  does this email ask me to DO something
                       -- a property of the text. User-independent. Stable.
  relevance      R(e)  is this about something I care about NOW
                       -- a property of the user today. Volatile.

    noise  : R < theta_rel
    fyi    : R >= theta_rel and A <  theta_act
    action : R >= theta_rel and A >= theta_act

Three labels were being cut out of a two-dimensional space, which is why the
cut was never clean. Now "track relevance and adjust the boundary" is a
well-posed operation on two scalars.

What does NOT happen here
-------------------------
No model is fine-tuned. Preference is a moving target and frozen weights are
the wrong shape of mechanism for one -- a professor matters less once the
course ends, and no amount of retraining keeps up with that. Everything the
language model does is in the stable row: what an email *says*. Everything the
user's behaviour touches lives here, updating one email at a time.

Cold start
----------
Day one runs entirely on what the user *declared* -- the active priorities
list. Learned weights start at exactly zero and earn influence only as evidence
arrives. The system begins with the user's own algorithm and gradually earns
the right to disagree with it. This is also what makes contaminated signal
harmless rather than fatal: a low-confidence update against a zero weight moves
almost nothing.

Dependencies
------------
None. `Informed Laziness` says stdlib before dependency, and the PA-II update
is five lines. A streaming-ML library would have brought drift detectors we do
not use yet; that is in DEFERRED at the bottom rather than in the import list.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

# --------------------------------------------------------------------------
# declared priors -- NOT learned
# --------------------------------------------------------------------------
# These are the day-one algorithm. They encode what the user told us (the
# priority list matters) and what is true of mail in general (a no-reply
# newsletter is not about your life). Learned weights are added on top and
# start at zero, so on the first run the ranking is exactly this and nothing
# else.

PRIOR_BIAS = -0.40           # an unmatched email is not noise, it is just not urgent
PRIOR_TOPIC = 2.20           # a full priority-list match dominates
PRIOR_STRUCTURAL_NOISE = -2.00

# Actionability is deliberately a FIXED formula. It describes the text, and the
# text does not change when the user changes. Nothing below learns.
ACT_BIAS = -1.30
ACT_MODEL_ACTION = 2.60      # the model said "action"
ACT_MODEL_NOISE = -1.10
ACT_SUBJECT_CUE = 1.00       # "action required", "RSVP", "deadline"...
ACT_BODY_CUE = 0.55
ACT_HAS_DEADLINE = 0.95
ACT_DIRECT_ASK = 0.70        # To: me, and a question mark
ACT_TASKS_FOUND = 1.15       # the extractor found concrete to-dos

DEFAULT_THETA_REL = 0.35
DEFAULT_THETA_ACT = 0.55

# PA-II. `epsilon` is a hinge tolerance: an error smaller than this produces NO
# update at all, which is what stops ordinary noise eroding a working ranking.
PA_EPSILON = 0.08
PA_MAX_STEP = 0.35           # no single email may swing a weight more than this

# Suppression is the expensive direction, so it is learned more slowly.
#
# PA-II treats both errors alike, and the audit showed what that costs: over
# five synthetic inboxes F1 rose on every one while recall fell as far as 0.59.
# The learner was buying precision by hiding things, and F1 -- a symmetric
# measure -- reported that as a win.
#
# In an inbox the two errors are not symmetric. Showing one thing you did not
# need is a second of annoyance. Hiding one thing you did need can cost a
# deadline, and it is self-concealing: a suppressed email is never shown, so it
# is never corrected, so the model never learns it was wrong. Same reasoning as
# Rocchio's beta=0.75 against gamma=0.15 elsewhere in this file.
PA_DEMOTE_SCALE = 0.85

# Confidence by signal provenance. This is where the contamination problem is
# actually solved: a QA click does not need to be *identified and excluded*, it
# only needs to arrive with a low C, and a low-C update against a zero-
# initialised weight moves almost nothing.
CONFIDENCE = {
    "explicit_correction": 1.00,   # promoted / demoted it by hand
    "mute": 0.90,                  # a standing decision about a correspondent
    "highlight": 0.90,
    "task_deleted": 0.85,          # a labelled extraction error
    "replied": 0.45,
    "flagged": 0.60,
    "opened_dwelled": 0.15,
    "opened": 0.05,
    "bulk": 0.00,
}

BURST_WINDOW_SECONDS = 90
BURST_MIN_ACTIONS = 5        # 5+ actions inside the window, across distinct senders
DWELL_FLOOR_MS = 1200        # opened and closed faster than this is not reading

EXPLORE_ONE_IN = 20          # ~5% of suppressed mail is surfaced anyway, labelled
EXPLORE_SALT = "fools-gold/explore/v1"

# Rocchio. Asymmetric on purpose: engagement is stronger evidence than its
# absence, so positive feedback pulls harder than negative pushes.
ROCCHIO_ALPHA = 1.00
ROCCHIO_BETA = 0.75
ROCCHIO_GAMMA = 0.15

THRESHOLD_STEP = 0.02
THRESHOLD_DAILY_CAP = 0.10   # one unusual morning must not reset the system
THRESHOLD_MIN = 0.05
THRESHOLD_MAX = 0.95
CONSISTENT_RUN = 3           # "marks in a consistent direction" -- how many

FEATURE_NAMES = (
    "topic_match",
    "direct_address",
    "affinity",
    "flagged",
    "answered",
    "highlighted",
    "unread",
    "attachment",
    "structural_noise",
)

_ACTION_CUE = re.compile(
    r"\b(action required|urgent|asap|deadline|due|reminder|rsvp|respond|reply|"
    r"confirm|approve|approval|sign|submit|complete|required|request(?:ed)?|"
    r"overdue|final notice|last chance to)\b", re.I
)
_NOISE_SENDER = re.compile(
    r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|newsletter|mailer|notifications?|"
    r"marketing|updates?|alerts?|billing|receipts?|support)@", re.I
)
_NOISE_SUBJECT = re.compile(
    r"\b(unsubscribe|newsletter|webinar|sale|% ?off|deal|promo|digest|"
    r"weekly round[- ]?up|invoice|receipt|order (?:confirm|ship))\b", re.I
)
_TOKEN = re.compile(r"[a-z0-9]+")


def sigmoid(x: float) -> float:
    if x < -60:
        return 0.0
    if x > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


# --------------------------------------------------------------------------
# typed state
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Thresholds:
    """The boundary, as an object that can be moved.

    `moved_today` exists so the daily cap is enforceable. Without it the
    consistent-direction rule below can be walked anywhere by one bad session.
    """
    relevance: float = DEFAULT_THETA_REL
    action: float = DEFAULT_THETA_ACT
    moved_today: float = 0.0
    moved_on: str = ""

    def clamped(self) -> "Thresholds":
        return Thresholds(
            relevance=min(THRESHOLD_MAX, max(THRESHOLD_MIN, self.relevance)),
            action=min(THRESHOLD_MAX, max(THRESHOLD_MIN, self.action)),
            moved_today=self.moved_today,
            moved_on=self.moved_on,
        )


@dataclass(frozen=True)
class Axes:
    """Both axes plus the feature vector that produced the relevance score.

    The features ride along because `Provenance As Precondition` applies to a
    score as much as to a claim: a number nobody can decompose is not a number
    anyone can argue with, and the learning update needs the same vector later.
    """
    actionability: float
    relevance: float
    features: dict[str, float] = field(default_factory=dict)

    def bucket(self, thresholds: Thresholds) -> str:
        if self.relevance < thresholds.relevance:
            return "noise"
        return "action" if self.actionability >= thresholds.action else "fyi"


@dataclass(frozen=True)
class Signal:
    """One piece of evidence about one email, before it is allowed to teach.

    A `Typed Action Contract`: it carries what it is, where it came from, and
    when -- and `apply_signal` may REFUSE it. Free-form updating has no
    equivalent; it always succeeds, so it can never tell you it should not have.
    """
    email_id: str
    kind: str                       # a key of CONFIDENCE
    target: float                   # 1.0 = should have ranked; 0.0 = should not
    at: str                         # ISO timestamp
    sender: str = ""
    dwell_ms: int = 0
    burst_index: int = 0            # position within a detected burst; 0 = alone


@dataclass(frozen=True)
class Outcome:
    """The result of offering a Signal to the model. A refusal is information,
    so it is returned rather than swallowed, and the caller records it."""
    applied: bool
    reason: str
    confidence: float = 0.0
    error: float = 0.0
    deltas: dict[str, float] = field(default_factory=dict)


# --------------------------------------------------------------------------
# features
# --------------------------------------------------------------------------

def _text_of(email: dict[str, Any]) -> str:
    return "%s\n%s" % (
        email.get("subject") or "",
        (email.get("body_text") or email.get("body_preview") or "")[:3000],
    )


def structural_noise(email: dict[str, Any]) -> float:
    sender = email.get("from_address") or ""
    subject = email.get("subject") or ""
    if _NOISE_SENDER.search(sender) or _NOISE_SUBJECT.search(subject):
        return 1.0
    return 0.0


def actionability(
    email: dict[str, Any],
    model_bucket: str | None = None,
    deadline: str | None = None,
    task_count: int = 0,
    addressed_directly: bool = False,
) -> float:
    """A(e): does this text ask the recipient to do something.

    Fixed weights, deliberately. This is the stable row of the timescale table
    -- the same email scores the same in January and in June, because the email
    did not change. The model's bucket is consumed as a *hint about the text*,
    never as a verdict about the user.
    """
    subject = email.get("subject") or ""
    body = (email.get("body_text") or email.get("body_preview") or "")[:2000]

    z = ACT_BIAS
    hint = (model_bucket or "").strip().lower()
    if hint == "action":
        z += ACT_MODEL_ACTION
    elif hint == "noise":
        z += ACT_MODEL_NOISE
    if _ACTION_CUE.search(subject):
        z += ACT_SUBJECT_CUE
    if _ACTION_CUE.search(body):
        z += ACT_BODY_CUE
    if deadline:
        z += ACT_HAS_DEADLINE
    if task_count > 0:
        z += ACT_TASKS_FOUND
    if addressed_directly and "?" in subject:
        z += ACT_DIRECT_ASK
    return round(sigmoid(z), 4)


def features_for(
    email: dict[str, Any],
    topic_match: float,
    addressed_directly: bool = False,
    affinity: float = 0.0,
    highlighted: bool = False,
) -> dict[str, float]:
    """The relevance feature vector. Every value in [0, 1] so one feature
    cannot dominate purely by scale -- PA-II divides by ||f||^2, which makes an
    unnormalised feature quietly steal the whole update."""
    return {
        "topic_match": max(0.0, min(1.0, float(topic_match))),
        "direct_address": 1.0 if addressed_directly else 0.0,
        "affinity": max(0.0, min(1.0, float(affinity))),
        "flagged": 1.0 if email.get("is_flagged") else 0.0,
        "answered": 1.0 if email.get("is_answered") else 0.0,
        "highlighted": 1.0 if highlighted else 0.0,
        "unread": 0.0 if email.get("is_read") else 1.0,
        "attachment": 1.0 if email.get("has_attachments") else 0.0,
        "structural_noise": structural_noise(email),
    }


def relevance(features: dict[str, float], weights: dict[str, float] | None = None) -> float:
    """R(e) = sigmoid(declared priors + learned deviation).

    With `weights` empty -- day one -- this is the priors alone, which is the
    user's own declared priority list and nothing the system invented.
    """
    w = weights or {}
    z = PRIOR_BIAS
    z += PRIOR_TOPIC * features.get("topic_match", 0.0)
    z += PRIOR_STRUCTURAL_NOISE * features.get("structural_noise", 0.0)
    for name, value in features.items():
        z += float(w.get(name, 0.0)) * float(value)
    return round(sigmoid(z), 4)


def axes_for(
    email: dict[str, Any],
    *,
    topic_match: float = 0.0,
    model_bucket: str | None = None,
    deadline: str | None = None,
    task_count: int = 0,
    addressed_directly: bool = False,
    affinity: float = 0.0,
    highlighted: bool = False,
    weights: dict[str, float] | None = None,
) -> Axes:
    feats = features_for(email, topic_match, addressed_directly, affinity, highlighted)
    return Axes(
        actionability=actionability(email, model_bucket, deadline, task_count, addressed_directly),
        relevance=relevance(feats, weights),
        features=feats,
    )


def derive_bucket(axes: Axes, thresholds: Thresholds | None = None) -> str:
    return axes.bucket(thresholds or Thresholds())


def explain(axes: Axes, thresholds: Thresholds, weights: dict[str, float] | None = None) -> dict[str, Any]:
    """Why this email landed where it did, decomposed.

    `Provenance As Precondition`: a derived belief that cannot name what
    produced it is not admissible. A ranking the user cannot interrogate is the
    same failure wearing a number.
    """
    w = weights or {}
    contributions = {"bias": round(PRIOR_BIAS, 4)}
    for name, value in axes.features.items():
        prior = 0.0
        if name == "topic_match":
            prior = PRIOR_TOPIC
        elif name == "structural_noise":
            prior = PRIOR_STRUCTURAL_NOISE
        total = (prior + float(w.get(name, 0.0))) * float(value)
        if abs(total) > 1e-9:
            contributions[name] = round(total, 4)
    return {
        "bucket": axes.bucket(thresholds),
        "actionability": axes.actionability,
        "relevance": axes.relevance,
        "theta_relevance": round(thresholds.relevance, 4),
        "theta_action": round(thresholds.action, 4),
        "contributions": contributions,
        "learned_weights": {k: round(v, 4) for k, v in sorted(w.items()) if abs(v) > 1e-9},
    }


# --------------------------------------------------------------------------
# learning -- the typed action, which may refuse
# --------------------------------------------------------------------------

def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def within_epoch(signal_at: str | None, epoch_start: str | None) -> bool:
    """No epoch set means learning has not been switched on. Refuse everything.

    Opt-in, not opt-out: the app spent weeks being clicked through for QA, and
    a default of "learn from whatever is already there" would have baked that
    in permanently on first run.
    """
    if not epoch_start:
        return False
    at, start = _parse_ts(signal_at), _parse_ts(epoch_start)
    if at is None or start is None:
        return False
    return at >= start


def detect_bursts(signals: Sequence[Signal]) -> list[Signal]:
    """Mark runs of rapid actions across distinct senders as a burst.

    Twelve actions in forty seconds across twelve unrelated correspondents is
    someone checking that the buttons work, not someone triaging. This stays
    true after the test phase -- everyone clicks around sometimes -- which is
    why it is detection rather than a date cutoff.
    """
    ordered = sorted(signals, key=lambda s: (_parse_ts(s.at) or datetime.min.replace(tzinfo=timezone.utc)))
    out: list[Signal] = [None] * len(ordered)  # type: ignore[list-item]
    index_of = {id(s): i for i, s in enumerate(ordered)}
    i = 0
    n = len(ordered)
    while i < n:
        j = i
        start = _parse_ts(ordered[i].at)
        senders = set()
        while j < n:
            at = _parse_ts(ordered[j].at)
            if start is None or at is None or (at - start).total_seconds() > BURST_WINDOW_SECONDS:
                break
            senders.add((ordered[j].sender or "").lower())
            j += 1
        run = ordered[i:j]
        # Rate is the signal. An earlier version also required BURST_MIN_ACTIONS
        # *distinct senders*, which sounded principled and was trivially evaded:
        # a real mailbox window often holds three correspondents, so clicking
        # through it at machine speed sailed past the guard. The audit caught it.
        #
        # What actually distinguishes legitimate speed from testing is
        # homogeneity: working through one sender with one gesture -- muting a
        # newsletter's forty messages -- is triage, and is spared. Mixed
        # gestures across mixed senders at that rate is someone checking that
        # the buttons work.
        kinds = {s.kind for s in run}
        homogeneous = len(senders) == 1 and len(kinds) == 1
        is_burst = len(run) >= BURST_MIN_ACTIONS and not homogeneous
        for offset, sig in enumerate(run):
            out[index_of[id(sig)]] = Signal(
                email_id=sig.email_id, kind=sig.kind, target=sig.target, at=sig.at,
                sender=sig.sender, dwell_ms=sig.dwell_ms,
                burst_index=(offset + 1) if is_burst else 0,
            )
        i = j
    return [s for s in out if s is not None]


def confidence_of(signal: Signal) -> tuple[float, str]:
    """How much this signal is allowed to teach, and why."""
    if signal.burst_index:
        return 0.0, "burst"
    base = CONFIDENCE.get(signal.kind)
    if base is None:
        return 0.0, "unknown-kind"
    if signal.kind in ("opened", "opened_dwelled") and signal.dwell_ms < DWELL_FLOOR_MS:
        return 0.0, "dwell-below-floor"
    return base, "ok"


def apply_signal(
    signal: Signal,
    features: dict[str, float],
    weights: dict[str, float],
    *,
    epoch_start: str | None,
) -> Outcome:
    """Offer one signal to the relevance model. May refuse; refusal is the point.

    PA-II (Crammer et al.), the rule Gmail's Priority Inbox uses:

        w_i <- w_i + f_i * sgn(e) * max(|e| - eps, 0) / (||f||^2 + 1/(2C))

    `eps` means a model that is already roughly right stops moving. `C` is
    per-example confidence, set by where the signal came from rather than by
    what it claims to be.
    """
    if not within_epoch(signal.at, epoch_start):
        return Outcome(False, "pre-epoch")
    conf, why = confidence_of(signal)
    if conf <= 0.0:
        return Outcome(False, why)

    predicted = relevance(features, weights)
    error = float(signal.target) - predicted
    if abs(error) <= PA_EPSILON:
        return Outcome(False, "within-hinge", confidence=conf, error=round(error, 4))

    norm_sq = sum(float(v) * float(v) for v in features.values())
    denom = norm_sq + 1.0 / (2.0 * conf)
    magnitude = (abs(error) - PA_EPSILON) / denom
    sign = 1.0 if error > 0 else -1.0
    if sign < 0:
        # Learning to hide is slower than learning to show. See PA_DEMOTE_SCALE.
        magnitude *= PA_DEMOTE_SCALE

    deltas: dict[str, float] = {}
    for name, value in features.items():
        if not value:
            continue
        step = sign * float(value) * magnitude
        step = max(-PA_MAX_STEP, min(PA_MAX_STEP, step))
        if abs(step) < 1e-9:
            continue
        weights[name] = round(float(weights.get(name, 0.0)) + step, 6)
        deltas[name] = round(step, 6)

    if not deltas:
        return Outcome(False, "no-active-features", confidence=conf, error=round(error, 4))
    return Outcome(True, "applied", confidence=conf, error=round(error, 4), deltas=deltas)


# --------------------------------------------------------------------------
# thresholds
# --------------------------------------------------------------------------

def adjust_threshold(
    thresholds: Thresholds,
    recent: Sequence[tuple[str, float]],
    *,
    today: date | None = None,
) -> tuple[Thresholds, str]:
    """The consistent-direction rule.

    Google did not compute their threshold -- users tuned it by hand, plus one
    automatic rule: when a user marks in a consistent direction, increment it in
    real time. Per-user thresholds cut their error 38% -> 31%, as much as the
    entire personalised model contributed (45% -> 38%). It is the cheapest thing
    in the design and the one with the most evidence behind it.

    `recent` is [(direction, actionability)] newest last, direction in
    {"promote", "demote"}.
    """
    today = today or date.today()
    stamp = today.isoformat()
    moved = thresholds.moved_today if thresholds.moved_on == stamp else 0.0

    if len(recent) < CONSISTENT_RUN:
        return Thresholds(thresholds.relevance, thresholds.action, moved, stamp), "too-few"
    window = list(recent)[-CONSISTENT_RUN:]
    directions = {d for d, _ in window}
    if len(directions) != 1:
        return Thresholds(thresholds.relevance, thresholds.action, moved, stamp), "not-consistent"

    direction = window[0][0]
    # Promotions that were already above the line teach nothing about the line.
    if direction == "promote":
        if not all(a < thresholds.action for _, a in window):
            return Thresholds(thresholds.relevance, thresholds.action, moved, stamp), "already-above"
        step = -THRESHOLD_STEP
    elif direction == "demote":
        if not all(a >= thresholds.action for _, a in window):
            return Thresholds(thresholds.relevance, thresholds.action, moved, stamp), "already-below"
        step = THRESHOLD_STEP
    else:
        return Thresholds(thresholds.relevance, thresholds.action, moved, stamp), "unknown-direction"

    if moved + abs(step) > THRESHOLD_DAILY_CAP + 1e-9:
        return Thresholds(thresholds.relevance, thresholds.action, moved, stamp), "daily-cap"

    return Thresholds(
        relevance=thresholds.relevance,
        action=thresholds.action + step,
        moved_today=round(moved + abs(step), 6),
        moved_on=stamp,
    ).clamped(), "moved"


def threshold_for_volume(
    actionabilities: Sequence[float],
    target_per_day: int,
    days: int = 1,
) -> float | None:
    """Solve for theta_act from a number the user actually knows.

    Nobody knows what 0.63 means. Everybody knows roughly how many things they
    want on today's list, so that is the control we expose and this is the
    arithmetic behind it.
    """
    scores = sorted((float(a) for a in actionabilities), reverse=True)
    if not scores or target_per_day <= 0 or days <= 0:
        return None
    wanted = max(1, int(round(target_per_day * days)))
    if wanted >= len(scores):
        return round(max(THRESHOLD_MIN, min(THRESHOLD_MAX, scores[-1])), 4)
    cut = scores[wanted - 1]
    nxt = scores[wanted]
    return round(max(THRESHOLD_MIN, min(THRESHOLD_MAX, (cut + nxt) / 2.0)), 4)


# --------------------------------------------------------------------------
# exploration
# --------------------------------------------------------------------------

def is_explored(email_id: str, one_in: int = EXPLORE_ONE_IN, salt: str = EXPLORE_SALT) -> bool:
    """Should this suppressed email be surfaced anyway, labelled as a guess?

    An oracle model -- one with perfect predictions -- maximises the speed of
    feedback-loop degeneracy. Accuracy is not protective: the more reliably the
    system shows what it already believes, the faster observed behaviour
    collapses onto that belief and the faster it stops learning. `noise` is a
    one-way door, so a fraction of it is opened on purpose.

    Deterministic on the id, not random: an email must not flicker in and out of
    the list between refreshes, and a test that cannot reproduce the selection
    cannot check it.

    **`salt` selects the population.** This is not decoration. The first version
    hashed the bare id, so the 1-in-20 was drawn over the whole mailbox and then
    filtered down to the suppressed part -- and the audit found a seed where all
    nine selected messages fell outside it, giving an exploration rate of zero
    with every unit test still green. Callers pass the bucket in the salt so the
    fraction is drawn *within* the population actually being suppressed.
    """
    # Order matters and did not, once: `one_in <= 1` was tested first, so the
    # documented way to switch exploration off (0) switched it fully ON.
    if one_in <= 0 or not email_id:
        return False
    if one_in == 1:
        return True
    digest = hashlib.blake2b(f"{salt}:{email_id}".encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % one_in == 0


def select_explored(
    email_ids: Iterable[str],
    one_in: int = EXPLORE_ONE_IN,
    salt: str = EXPLORE_SALT,
) -> set[str]:
    """Pick the explored subset of a suppressed population as a QUOTA, not a coin flip.

    Per-email sampling is what the audit caught: over 74 suppressed messages at
    1-in-20 the expected count is 3.7, and the probability of drawing *zero* is
    about 2% -- so a user with a quiet inbox could receive no exploration at all
    while every unit test stayed green and the bulk rate looked perfect.

    "Roughly 5% of suppressed mail, and never nothing when there is suppressed
    mail" is the promise the feature actually makes, so it is expressed here
    rather than left to luck. Selection is still by hash, so it is stable
    between refreshes and reproducible in a test.
    """
    ids = [str(i) for i in email_ids if i]
    if not ids or one_in <= 0:
        return set()
    if one_in == 1:
        return set(ids)
    wanted = max(1, round(len(ids) / one_in))
    ranked = sorted(ids, key=lambda i: hashlib.blake2b(
        f"{salt}:{i}".encode("utf-8"), digest_size=8).digest())
    return set(ranked[:wanted])


def exploration_rate(email_ids: Iterable[str], one_in: int = EXPLORE_ONE_IN) -> float:
    ids = list(email_ids)
    if not ids:
        return 0.0
    return sum(1 for i in ids if is_explored(i, one_in)) / len(ids)


# --------------------------------------------------------------------------
# topic centroids (Rocchio) over a pluggable embedder
# --------------------------------------------------------------------------

def hashing_embed(text: str, dims: int = 256) -> list[float]:
    """A deterministic bag-of-words vector, so the centroid machinery works
    today with no model and no download.

    This is the placeholder EmbeddingGemma replaces: swap `set_embedder` and
    every centroid, cosine and Rocchio update below keeps working unchanged.
    Building the drift mechanism against a real embedder we have not shipped
    yet would have made none of it testable.
    """
    vec = [0.0] * dims
    for token in _TOKEN.findall((text or "").lower()):
        if len(token) < 3:
            continue
        h = hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest()
        idx = int.from_bytes(h, "big") % dims
        vec[idx] += 1.0
    return normalise(vec)


_embedder: Callable[[str], list[float]] = hashing_embed


def set_embedder(fn: Callable[[str], list[float]] | None) -> None:
    global _embedder
    _embedder = fn or hashing_embed


def embed(text: str) -> list[float]:
    return _embedder(text)


def normalise(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(float(v) * float(v) for v in vec))
    if norm <= 0:
        return [0.0] * len(vec)
    return [round(float(v) / norm, 8) for v in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return round(max(-1.0, min(1.0, sum(float(x) * float(y) for x, y in zip(a, b)))), 6)


def rocchio(
    centroid: Sequence[float],
    engaged: Sequence[Sequence[float]] = (),
    rejected: Sequence[Sequence[float]] = (),
) -> list[float]:
    """Move a topic's centre toward what the user engaged with, away from what
    they rejected.

    This is how "the professor matters less once the course ends" resolves with
    no one editing anything: the centroid stops being fed, and the topic drifts
    out of the region the new mail occupies.
    """
    dims = len(centroid)
    out = [ROCCHIO_ALPHA * float(v) for v in centroid]
    if engaged:
        mean = _mean(engaged, dims)
        out = [o + ROCCHIO_BETA * m for o, m in zip(out, mean)]
    if rejected:
        mean = _mean(rejected, dims)
        out = [o - ROCCHIO_GAMMA * m for o, m in zip(out, mean)]
    return normalise(out)


def _mean(vectors: Sequence[Sequence[float]], dims: int) -> list[float]:
    acc = [0.0] * dims
    count = 0
    for vec in vectors:
        if len(vec) != dims:
            continue
        for i, v in enumerate(vec):
            acc[i] += float(v)
        count += 1
    if not count:
        return acc
    return [v / count for v in acc]


def topic_match(
    email: dict[str, Any],
    centroids: dict[str, Sequence[float]],
    keyword_hits: Iterable[str] = (),
    floor: float = 0.0,
) -> tuple[float, list[str]]:
    """Best cosine against any active topic, with keyword hits as a floor.

    The keyword floor is what keeps a declared topic registering on day one,
    before any centroid has been moved by anything -- and if the embedder is
    ever unavailable, the list still sorts by what the user said they cared
    about rather than collapsing to zero.
    """
    hits = {str(h).strip().lower() for h in keyword_hits if str(h).strip()}
    best, matched = floor, sorted(hits)
    if centroids:
        vec = embed(_text_of(email))
        for topic, centroid in centroids.items():
            score = cosine(vec, centroid)
            if score > best:
                best, matched = score, [topic]
            elif score > 0 and topic.lower() in hits and topic not in matched:
                matched.append(topic)
    if hits and best < 1.0:
        best = max(best, 0.85)     # an explicit keyword match is near-certain
    return round(max(0.0, min(1.0, best)), 4), matched


# --------------------------------------------------------------------------
# DEFERRED -- noticed, not taken, recorded rather than left as silent debt
# --------------------------------------------------------------------------
# 1. Drift detection (ADWIN / Page-Hinkley) to ask "your priorities changed?"
#    rather than waiting for the user to notice. Needs a streaming-ML
#    dependency or ~120 lines; neither is justified before the online weights
#    have run against real mail.
# 2. EmbeddingGemma in place of `hashing_embed`. The seam is `set_embedder`.
# 3. A calibrated actionability probability from model logprobs, or a small
#    encoder head. The fixed formula here is a stand-in that is at least
#    monotone and inspectable.
# 4. Per-priority activity decay -- a topic untouched for N days should
#    surface as "still relevant?" in Settings. Rocchio handles the drift; it
#    does not handle the asking.
