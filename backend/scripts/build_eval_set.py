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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, quality  # noqa: E402

# The sampling rule lives in app/quality.py so the in-app quality check and
# this script can never drift apart. See it for why the threshold is 8 Hangul
# characters and why strata are filled round-robin.
language_of = quality.language_of


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

    with db.connect() as conn:
        if not conn.execute("SELECT 1 FROM emails LIMIT 1").fetchone():
            print("No mail in the database. Sync the mailbox first.")
            return 1
    res = quality.sample(args.n, seed=args.seed, reset=args.reset)

    print(f"{res['mailbox']} emails in the mailbox; {res['already_labelled']} already labelled.")
    print(f"sampled {res['sampled']} for labelling (target {args.n}).\n")
    print("  by language: " + ", ".join(f"{k}={v}" for k, v in sorted(res["by_language"].items())))
    print("  by stratum:")
    for k, v in sorted(res["by_stratum"].items()):
        print(f"     {k:<28} {v}")
    print("\nNext: .venv/bin/python scripts/label_eval_set.py  (or Settings > Quality check)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
