#!/usr/bin/env python3
"""Sample the user's own mail into an evaluation set.

Why this exists: there is no published Korean benchmark for Qwen3.5 or Gemma 4
at any size. I looked for one and could not find it, so every model choice in
`claude/slm-plan.md` below the packaging level is an extrapolation from English
and European benchmarks. This mailbox is the only ground truth available, and
it is also the right one -- it is the distribution the app actually runs on.

    cd backend && .venv/bin/python scripts/build_eval_set.py --n 120

Nothing leaves the machine. Rows are written into `eval_labels` in the user's
own database, beside the mail they describe. This script prints counts and
never prints a subject line, so it is safe to run in a shared terminal and safe
to paste the output anywhere.

Stratified rather than random, because a uniform sample of a real inbox is
mostly newsletters, and a set that is 70% noise measures almost nothing about
the two buckets that matter. Strata:

    language     ko / en   -- a model that quietly degrades on Korean is the
                             specific failure this whole set exists to catch
    bucket       action / fyi / noise, as currently classified
    deadline     with / without

The current classification is used ONLY to spread the sample. It is not ground
truth and is never copied into a label -- that would train the eval set on the
thing it is meant to judge.
"""
from __future__ import annotations

import argparse
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402

# Hangul syllables, plus Jamo. Deliberately crude: this decides a stratum, not
# a label, and a wrong call costs a slightly uneven sample rather than a wrong
# measurement.
_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
_LATIN = re.compile(r"[A-Za-z]")


# Measured on the real mailbox before this threshold was chosen, rather than
# guessed: 1,218 emails, of which **37 contain any Hangul at all (3.0%)** and
# only 8 are Korean-dominant. A "which script wins" rule therefore labelled 29
# of those 37 as English and the sample reached one Korean email.
#
# What this app actually needs to know is not which language an email is *in*.
# It is whether the model handles Hangul content correctly -- a Korean deadline
# sentence inside an otherwise English departmental notice is exactly the case
# that breaks, and exactly the case a dominance rule throws away.
_KO_MIN_CHARS = 8


def language_of(email: dict) -> str:
    text = f"{email.get('subject') or ''} {(email.get('body_text') or '')[:400]}"
    ko, en = len(_HANGUL.findall(text)), len(_LATIN.findall(text))
    if ko > en:
        return "ko"
    if ko >= _KO_MIN_CHARS:
        return "mixed"
    return "en"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=120,
                    help="target size. 120 sits above the 93-item held-out split "
                         "used by the closest published small-corpus study.")
    ap.add_argument("--seed", type=int, default=20260919,
                    help="fixed, so re-running produces the same set rather than "
                         "quietly growing a different one")
    ap.add_argument("--reset", action="store_true",
                    help="drop UNLABELLED rows and re-sample. Never touches a label.")
    args = ap.parse_args()
    rng = random.Random(args.seed)

    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT e.id, e.subject, e.body_text, e.received_at, "
            "       c.bucket, c.deadline "
            "FROM emails e LEFT JOIN classifications c ON c.email_id = e.id"
        ).fetchall()]
        already = {r["email_id"] for r in conn.execute(
            "SELECT email_id FROM eval_labels WHERE bucket IS NOT NULL")}
        if args.reset:
            conn.execute("DELETE FROM eval_labels WHERE bucket IS NULL")
            conn.commit()

    if not rows:
        print("No mail in the database. Sync the mailbox first.")
        return 1

    buckets: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        if row["id"] in already:
            continue
        key = (language_of(row),
               row["bucket"] or "unclassified",
               "dated" if row["deadline"] else "undated")
        buckets[key].append(row)

    # Round-robin across strata so a small stratum is not drowned by a large
    # one. A mailbox that is 80% English still contributes its Korean mail.
    order = sorted(buckets, key=lambda k: (-len(buckets[k]), k))
    for key in order:
        rng.shuffle(buckets[key])
    picked, i = [], 0
    while len(picked) < args.n - len(already) and any(buckets[k] for k in order):
        key = order[i % len(order)]
        if buckets[key]:
            picked.append((buckets[key].pop(), key))
        i += 1

    with db.connect() as conn:
        for row, key in picked:
            conn.execute(
                "INSERT OR IGNORE INTO eval_labels (email_id, language, stratum) "
                "VALUES (?, ?, ?)", (row["id"], key[0], "/".join(key)))
        conn.commit()

    tally = Counter(k[0] for _, k in picked)
    strata = Counter("/".join(k) for _, k in picked)
    print(f"{len(rows)} emails in the mailbox; {len(already)} already labelled.")
    print(f"sampled {len(picked)} for labelling (target {args.n}).\n")
    print("  by language: " + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    print("  by stratum:")
    for k, v in sorted(strata.items()):
        print(f"     {k:<28} {v}")
    print("\nNext: .venv/bin/python scripts/label_eval_set.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
