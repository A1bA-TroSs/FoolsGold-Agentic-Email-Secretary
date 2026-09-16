#!/usr/bin/env python3
"""Objective audit of the adaptive ranking layer.

Unit tests check that each rule does what it says. This checks something the
unit tests cannot: that the rules, composed, produce a ranking that gets
BETTER, and that the guards actually stop it getting worse.

Method. A synthetic mailbox with known ground truth, a simulated user who acts
according to that truth, and a held-out set the learner never sees. Then four
claims, each with a negative control, because a check that can only pass is not
a check:

  A. Cold start ranks on the declared list alone, with no learned weights.
  B. Real corrections improve F1 on held-out mail.
  C. Contaminated signal -- bursts, pre-epoch rows -- does NOT move the model.
  D. Exploration surfaces suppressed mail at the configured rate, and never a
     muted sender.

Run it. Note this repo has TWO `scripts/` directories -- one at the root and
this one under `backend/` -- so the bare relative path is ambiguous and picks
the wrong one from the repo root:

    cd backend && .venv/bin/python scripts/audit_ranking.py

Any working directory works (the script resolves its own path), but it needs the
backend environment: `python3` without the venv fails on `httpx`. `pytest` also
runs this whole audit via `tests/test_audit_ranking.py`, so there is nothing
extra to remember -- run it directly only when you want to read the report.

Exit: 0 when every claim passes, 1 otherwise. Nothing is rounded into passing.
"""
from __future__ import annotations

import os
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, learning, relevance  # noqa: E402
from app.relevance import Signal, Thresholds  # noqa: E402

SEED = 20260915
TOPICS = ["thesis", "internship", "lab funding"]

# Three clean classes plus two confusable ones. The first version of this audit
# had only the clean classes, and the learner scored a held-out F1 of exactly
# 1.0 -- because the topic keyword separated the labels perfectly by
# construction. A perfect number on a separable toy is a vacuous pass: it proves
# the update rule can fit a line, not that the design works. So:
#
#   VEILED    the user wants it, and the topic word is absent -- only the
#             sender and their own flags say so. Learnable, but not from
#             keywords, which is the whole point of having learned weights.
#   BAITED    the topic word is present and the user does NOT want it.
#             Promotional mail that name-drops. Keyword matching gets these
#             exactly wrong, so the priors alone must score below ceiling.
#
# With both present, F1 = 1.0 is unreachable, and B3 asserts that it is.
RELEVANT_SUBJECTS = [
    "thesis defence scheduling", "thesis chapter three feedback",
    "internship interview confirmation", "internship offer paperwork",
    "lab funding renewal form", "lab funding committee minutes",
]
VEILED_SUBJECTS = [
    "about Tuesday", "the draft you sent", "re: our conversation",
    "quick question before Friday",
]
IRRELEVANT_SUBJECTS = [
    "gym membership renewal", "campus parking survey",
    "cafeteria menu this week", "alumni photo contest",
]
BAITED_SUBJECTS = [
    "thesis printing services -- 30% off", "internship fair sponsors wanted",
    "lab funding webinar: register now",
]
NOISE_SUBJECTS = [
    "Weekly round-up: 40% off boots", "Your receipt from ShopCo",
    "Webinar: scale your startup", "Unsubscribe preferences updated",
]


def make_mailbox(n: int = 240, seed: int = SEED) -> list[dict]:
    """Ground truth is `want`: would the user want this ranked? The generator
    knows; nothing downstream is told."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        kind = rng.choices(
            ["relevant", "veiled", "irrelevant", "baited", "noise"],
            weights=[0.24, 0.14, 0.24, 0.12, 0.26],
        )[0]
        if kind == "relevant":
            subject, sender, want = rng.choice(RELEVANT_SUBJECTS), "lee@uni.edu", 1
        elif kind == "veiled":
            subject, sender, want = rng.choice(VEILED_SUBJECTS), "lee@uni.edu", 1
        elif kind == "irrelevant":
            subject, sender, want = rng.choice(IRRELEVANT_SUBJECTS), "admin@uni.edu", 0
        elif kind == "baited":
            subject, sender, want = rng.choice(BAITED_SUBJECTS), "promo@vendor.com", 0
        else:
            subject, sender, want = rng.choice(NOISE_SUBJECTS), "noreply@shop.com", 0
        out.append({
            "id": f"m{i:04d}", "subject": subject, "from_address": sender,
            "from_name": sender.split("@")[0], "to_recipients": '["me@x.com"]',
            "cc_recipients": "[]", "body_text": f"{subject}. Details follow.",
            "is_read": rng.random() < 0.5, "is_answered": 0,
            "is_flagged": 1 if (want and rng.random() < 0.2) else 0,
            "has_attachments": 0, "received_at": "2026-09-10T09:00:00+00:00",
            "_want": want, "_kind": kind,
        })
    return out


def features(email: dict, centroids: dict) -> dict[str, float]:
    hits = [t for t in TOPICS if t in (email["subject"] or "").lower()]
    match, _ = relevance.topic_match(email, centroids, keyword_hits=hits)
    return relevance.features_for(email, topic_match=match, addressed_directly=True)


def average_precision(mailbox: list[dict], weights: dict, centroids: dict) -> float:
    """Ranking quality with no threshold involved.

    The first version of this audit measured F1 at a frozen theta, and the
    numbers were nonsense in a way that took a parameter sweep to see: learning
    shifts the whole relevance distribution, so a fixed cut slices a moving
    target and the "recall collapse" was the cut sitting in a different place,
    not the model hiding things. Average precision asks the only question that
    is actually about learning -- is wanted mail ranked above unwanted mail --
    and leaves the boundary to the part of the design that owns it.
    """
    scored = sorted(
        ((relevance.relevance(features(e, centroids), weights), e["_want"]) for e in mailbox),
        key=lambda pair: -pair[0],
    )
    total = sum(want for _, want in scored)
    if not total:
        return 0.0
    hits = 0
    acc = 0.0
    for rank, (_, want) in enumerate(scored, start=1):
        if want:
            hits += 1
            acc += hits / rank
    return round(acc / total, 4)


def measure(mailbox: list[dict], weights: dict, centroids: dict, theta: float) -> dict:
    tp = fp = fn = 0
    for email in mailbox:
        predicted = 1 if relevance.relevance(features(email, centroids), weights) >= theta else 0
        if predicted and email["_want"]:
            tp += 1
        elif predicted and not email["_want"]:
            fp += 1
        elif not predicted and email["_want"]:
            fn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"f1": round(f1, 4), "precision": round(precision, 4),
            "recall": round(recall, 4), "tp": tp, "fp": fp, "fn": fn}


def run_experiment(seed: int) -> dict:
    """One full cold-start -> learn -> measure cycle on an isolated database.

    Returns metrics rather than asserting, so the claims below can be made over
    a distribution of seeds instead of over whichever one happened to be
    hardcoded. A single seed proves a single sample; the design is supposed to
    work on inboxes in general.
    """
    tmp = Path(tempfile.mkdtemp())
    db.DATA_DIR, db.DB_PATH = tmp, tmp / f"seed{seed}.db"
    db.init_db()
    with db.connect() as conn:
        for topic in TOPICS:
            conn.execute(
                "INSERT INTO priorities (topic, status, weight, source, created_at) "
                "VALUES (?, 'active', 20, 'user', ?)", (topic, db.now_iso()))
        conn.commit()
    learning.seed_centroids()
    centroids = db.priority_centroids(learning.EMBEDDER_NAME)
    learning.begin_epoch("2026-09-01T00:00:00+00:00")

    mailbox = make_mailbox(seed=seed)
    rng = random.Random(seed + 1)
    rng.shuffle(mailbox)
    train, held_out = mailbox[:160], mailbox[160:]
    theta = Thresholds().relevance

    before_ap = average_precision(held_out, {}, centroids)
    feats = {e["id"]: features(e, centroids) for e in train}

    signals, minute = [], 0
    for email in train:
        if rng.random() > 0.34:
            continue
        minute += rng.randint(3, 9)
        day, hour, mins = 10 + minute // 1440, (minute // 60) % 24, minute % 60
        signals.append(Signal(
            email_id=email["id"], kind="explicit_correction",
            target=float(email["_want"]),
            at=f"2026-09-{day:02d}T{hour:02d}:{mins:02d}:00+00:00",
            sender=email["from_address"],
        ))
    result = learning.learn(signals, feats)
    weights = db.ranking_weights()

    # Both arms are evaluated at the SAME operating point, solved the way the
    # product solves it: the user names a volume and theta follows. Calibrated
    # on train and applied to held-out -- calibrating on the test set is how an
    # audit reports a number it has not earned.
    #
    # Note what this exposed. The default theta of 0.35 surfaces almost
    # everything, because an unmatched email scores sigmoid(-0.4) = 0.40 and
    # only structural noise falls below the line. On day one that is arguably
    # right -- do not hide mail you know nothing about -- but it means F1 at the
    # default threshold measures the base rate, not the ranking. Hence the
    # volume control, and hence average precision as the learning metric.
    target = sum(e["_want"] for e in train) or 1
    theta_before = relevance.threshold_for_volume(
        [relevance.relevance(feats[e["id"]], {}) for e in train], target) or theta
    theta_after = relevance.threshold_for_volume(
        [relevance.relevance(feats[e["id"]], weights) for e in train], target) or theta

    before = measure(held_out, {}, centroids, theta_before)
    after = measure(held_out, weights, centroids, theta_after)
    after_ap = average_precision(held_out, weights, centroids)
    return {"seed": seed, "before": before, "after": after,
            "before_ap": before_ap, "after_ap": after_ap,
            "theta_before": round(theta_before, 4), "theta_after": round(theta_after, 4),
            "applied": result["applied"], "offered": result["offered"],
            "wanted": sum(e["_want"] for e in held_out), "held_out": len(held_out)}


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[bool, str, str]] = []

    def check(self, ok: bool, claim: str, detail: str = "") -> None:
        self.rows.append((bool(ok), claim, detail))

    def guard(self, claim: str):
        """Run a claim's setup so that an exception becomes a FAILED row.

        Found the hard way: a missing dependency threw inside main() after nine
        claims had already been evaluated, and the audit printed *nothing at
        all* -- no report, no partial result, just a traceback. An audit that
        can die without reporting is the same silent-failure class it exists to
        catch, so a claim that explodes is now a claim that failed, and the
        other eleven still print.
        """
        report = self

        class _Guard:
            def __enter__(self):
                return report

            def __exit__(self, exc_type, exc, _tb):
                if exc_type is None:
                    return False
                report.check(False, claim, f"raised {exc_type.__name__}: {exc}")
                return True

        return _Guard()

    def render(self) -> bool:
        width = max(len(c) for _, c, _ in self.rows) + 2
        print("-" * (width + 8))
        for ok, claim, detail in self.rows:
            print(f"{claim:<{width}} {'PASS' if ok else 'FAIL'}")
            if detail:
                print(f"    {detail}")
        print("-" * (width + 8))
        passed = sum(1 for ok, _, _ in self.rows if ok)
        print(f"claims: {passed}/{len(self.rows)}")
        return passed == len(self.rows)


def main() -> int:
    report = Report()

    # ---------------------------------------------------------------- B ---
    # Learning, measured over a distribution of inboxes rather than one seed.
    runs = [run_experiment(SEED + k) for k in range(5)]
    improved = [r for r in runs if r["after"]["f1"] > r["before"]["f1"]]
    mean_before = sum(r["before"]["f1"] for r in runs) / len(runs)
    mean_after = sum(r["after"]["f1"] for r in runs) / len(runs)

    report.check(
        all(20 <= r["wanted"] <= r["held_out"] - 20 for r in runs),
        "0. every held-out set is balanced enough to measure",
        "; ".join(f"{r['seed']}: {r['wanted']}/{r['held_out']}" for r in runs),
    )
    report.check(
        all(r["applied"] > 0 for r in runs),
        "B1. deliberate corrections are accepted",
        "; ".join(f"{r['applied']}/{r['offered']}" for r in runs),
    )

    # Average precision, not F1 at a fixed cut. Learning shifts the whole
    # relevance distribution, so a frozen threshold measures where the cut
    # landed rather than whether the ranking improved -- an earlier version of
    # this audit reported a "recall collapse" that was entirely that artifact.
    deltas = [r["after_ap"] - r["before_ap"] for r in runs]
    mean_gain = sum(deltas) / len(deltas)
    improved = sum(1 for d in deltas if d > 0)
    report.check(
        improved >= 4 and mean_gain > 0.02 and min(deltas) > -0.02,
        "B2. learning improves ranking quality, and says where it does not",
        f"AP improved on {improved}/{len(runs)} seeds, mean {mean_gain:+.4f}, "
        f"worst {min(deltas):+.4f}  "
        + " | ".join(f"{r['before_ap']}->{r['after_ap']}" for r in runs),
    )
    report.check(
        all(r["after_ap"] < 0.98 for r in runs),
        "B3. the task is hard enough that B2 is not a vacuous pass",
        "a separable toy scores 1.0 and proves only that the update rule fits a "
        "line; the veiled and baited classes cap it. "
        + " | ".join(str(r["after_ap"]) for r in runs),
    )

    # At the operating point the product actually uses -- theta solved from a
    # volume, calibrated on train, applied to held-out.
    worst_recall = min(r["after"]["recall"] for r in runs)
    recall_drop = max(r["before"]["recall"] - r["after"]["recall"] for r in runs)
    mean_f1_before = sum(r["before"]["f1"] for r in runs) / len(runs)
    mean_f1_after = sum(r["after"]["f1"] for r in runs) / len(runs)
    report.check(
        worst_recall >= 0.70 and recall_drop <= 0.30,
        "B4. precision is not bought by throwing recall away",
        f"worst recall {worst_recall:.3f}, largest drop {recall_drop:.3f}; "
        f"mean F1 {mean_f1_before:.4f} -> {mean_f1_after:.4f}. "
        f"In an inbox a false negative costs more than a false positive and is "
        f"self-concealing, so this is checked separately from F1.",
    )

    # ------------------------------------------------------------- A/C/D/E --
    # One isolated database for the guard claims, which are about mechanism
    # rather than about statistics.
    tmp = Path(tempfile.mkdtemp())
    db.DATA_DIR, db.DB_PATH = tmp, tmp / "guards.db"
    db.init_db()
    with db.connect() as conn:
        for topic in TOPICS:
            conn.execute(
                "INSERT INTO priorities (topic, status, weight, source, created_at) "
                "VALUES (?, 'active', 20, 'user', ?)", (topic, db.now_iso()))
        conn.commit()
    learning.seed_centroids()
    centroids = db.priority_centroids(learning.EMBEDDER_NAME)
    mailbox = make_mailbox()
    train, held_out = mailbox[:160], mailbox[160:]
    theta = Thresholds().relevance
    feats = {e["id"]: features(e, centroids) for e in train}

    base = measure(held_out, {}, centroids, theta)
    report.check(
        db.ranking_weights() == {} and base["tp"] > 0,
        "A. cold start ranks on the declared list alone",
        f"no learned weights; F1={base['f1']} tp={base['tp']} fp={base['fp']}",
    )

    learning.begin_epoch("2026-09-01T00:00:00+00:00")
    contaminated = [
        Signal(e["id"], "explicit_correction", 1.0,
               f"2026-09-10T09:00:{i * 2:02d}+00:00", e["from_address"])
        for i, e in enumerate(train[:12])
    ] + [
        Signal(e["id"], "explicit_correction", 1.0,
               "2026-08-01T09:00:00+00:00", e["from_address"])
        for e in train[12:24]
    ]
    junk = learning.learn(contaminated, feats)
    after_junk = measure(held_out, db.ranking_weights(), centroids, theta)
    report.check(
        junk["applied"] == 0 and db.ranking_weights() == {}
        and after_junk["f1"] == base["f1"],
        "C1. contaminated signal changes nothing",
        f"offered {junk['offered']}, applied {junk['applied']}, "
        f"refused {junk['refused']}; F1 {base['f1']} -> {after_junk['f1']}",
    )

    # Teach it properly, then attack it.
    rng = random.Random(SEED + 99)
    honest, minute = [], 0
    for email in train:
        if rng.random() > 0.34:
            continue
        minute += rng.randint(3, 9)
        honest.append(Signal(
            email["id"], "explicit_correction", float(email["_want"]),
            f"2026-09-{10 + minute // 1440:02d}T{(minute // 60) % 24:02d}:{minute % 60:02d}:00+00:00",
            email["from_address"],
        ))
    learning.learn(honest, feats)
    taught = dict(db.ranking_weights())
    attack = [
        Signal(e["id"], "explicit_correction", 0.0,
               f"2026-09-30T09:00:{i * 2:02d}+00:00", e["from_address"])
        for i, e in enumerate(train[:12])
    ]
    attacked = learning.learn(attack, feats)
    report.check(
        taught != {} and attacked["applied"] == 0 and db.ranking_weights() == taught,
        "C2. a burst cannot undo what deliberate use taught",
        f"learned {len(taught)} weights; attack applied {attacked['applied']}, "
        f"refused {attacked['refused']}",
    )

    suppressed = [e["id"] for e in mailbox if e["_kind"] == "noise"]
    one_in = learning.explore_one_in()
    explored_here = sorted(learning.explored_in_batch(suppressed))
    bulk = relevance.exploration_rate([f"synthetic-{i}" for i in range(6000)], one_in=one_in)
    report.check(
        len(suppressed) > 40 and 0.03 < bulk < 0.07,
        "D1. the sampler is unbiased at the configured rate",
        f"1 in {one_in} over 6000 ids -> {bulk:.4f}",
    )
    report.check(
        0 < len(explored_here) <= max(2, len(suppressed) // 5),
        "D2. the suppressed set really is sampled, never zero",
        f"{len(explored_here)} of {len(suppressed)} explored "
        f"({len(explored_here) / len(suppressed):.3f}) -- a quota, not a coin flip: "
        f"P(zero) at 1-in-{one_in} over {len(suppressed)} messages is ~2%",
    )

    with report.guard("D3. exploration opens the system's guesses, never the user's mute"):
        _explore_probe(report, mailbox)

    summary = db.learning_summary()
    report.check(
        summary["applied"] > 0 and summary["refused"] > 0 and summary["refused_by_reason"],
        "E. every decision, refusals included, reached the audit trail",
        f"applied {summary['applied']}, refused {summary['refused']} "
        f"{summary['refused_by_reason']}",
    )

    return 0 if report.render() else 1


def _explore_probe(report: "Report", mailbox: list[dict]) -> None:
    """The one claim that needs the whole pipeline, and therefore the whole
    dependency set. Isolated so its imports cannot take the report down."""
    from app import pipeline
    from app.llm.base import Classification

    db.set_setting("explore_one_in", "1")
    muted_sender, open_sender = "noreply@shop.com", "promo@other.com"
    db.mute_sender(muted_sender)
    probes = [{
        "id": f"probe{idx}", "conversation_id": None,
        "subject": "Weekly round-up: 40% off boots", "from_name": "promo",
        "from_address": sender, "to_recipients": "[]", "cc_recipients": "[]",
        "received_at": "2026-09-10T09:00:00+00:00", "is_read": 0, "is_answered": 0,
        "is_flagged": 0, "has_attachments": 0, "importance": "normal", "web_link": "",
        "folder": "INBOX", "body_preview": "", "body_text": "Sale ends soon",
        "body_html": "", "synced_at": db.now_iso(),
    } for idx, sender in ((0, muted_sender), (1, open_sender))]
    db.upsert_emails(probes)
    by_id = {e["id"]: e for e in db.get_emails(["probe0", "probe1"])}
    pipeline._persist(
        [Classification(email_id=pid, bucket="noise", deadline=None, rationale="promo")
         for pid in ("probe0", "probe1")],
        by_id, source="structural", model="audit",
    )
    with db.connect() as conn:
        flags = {r["email_id"]: r["explored"] for r in conn.execute(
            "SELECT email_id, explored FROM classifications "
            "WHERE email_id IN ('probe0','probe1')").fetchall()}
    report.check(
        flags.get("probe0") == 0 and flags.get("probe1") == 1,
        "D3. exploration opens the system's guesses, never the user's mute",
        f"muted probe explored={flags.get('probe0')}, "
        f"unmuted probe explored={flags.get('probe1')} "
        f"-- both must be present or this claim is vacuous",
    )


if __name__ == "__main__":
    raise SystemExit(main())
