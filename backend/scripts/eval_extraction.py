#!/usr/bin/env python3
"""Score a provider against the hand-labelled set. Per language, never averaged.

    .venv/bin/python scripts/eval_extraction.py                 # configured provider
    .venv/bin/python scripts/eval_extraction.py --provider none # the structural floor
    .venv/bin/python scripts/eval_extraction.py --model qwen3.5:4b

Three rules this file is built around, each of which cost something to learn:

**Report Korean and English separately, always.** An average over a mixed
mailbox hides a model that is fine in English and poor in Korean, which is the
single most likely failure for every candidate model and the one no published
benchmark covers.

**Always score `none` as well.** Structural regex is the floor. A model that
does not beat it is not earning its 6GB, and without the floor on the same
table every model number looks impressive.

**Report the negative cases separately.** Over-extraction from newsletters is
this app's documented failure mode -- it is what turned the calendar into a
dumping ground. Precision on "this email contains no task" is therefore a
first-class number here, not something folded into an F1.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, priority  # noqa: E402
from app.llm.base import ProviderUnavailable  # noqa: E402

BUCKETS = ("action", "fyi", "noise")


def labelled() -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT l.*, e.subject, e.from_name, e.from_address, e.body_text, "
            "       e.body_preview, e.to_recipients, e.cc_recipients, e.received_at, "
            "       e.is_read, e.is_answered, e.is_flagged, e.has_attachments, e.importance "
            "FROM eval_labels l JOIN emails e ON e.id = l.email_id "
            "WHERE l.bucket IS NOT NULL")]


async def predict(rows: list[dict], provider_name: str, model: str, host: str):
    topics = priority.priority_topics()
    if provider_name == "none":
        # The same function the app uses when no provider is configured, not a
        # re-implementation of it: a floor computed differently from the real
        # floor is not a floor.
        from app import pipeline
        started = time.monotonic()
        me = db.get_setting("user_address", "")
        got = pipeline._structural_classifications(
            [dict(r, id=r["email_id"]) for r in rows], me)
        return {c.email_id: c for c in got}, time.monotonic() - started
    if provider_name == "ollama":
        from app.llm.ollama_provider import OllamaProvider
        provider = OllamaProvider(model or db.get_setting("ollama_model", ""), host)
    else:
        from app.llm import registry
        provider = registry.get_provider()

    size = int(db.get_setting("classify_batch_size", "10") or 10)
    out, started = {}, time.monotonic()
    for i in range(0, len(rows), size):
        chunk = rows[i:i + size]
        try:
            for c in await provider.classify_batch(chunk, topics):
                out[c.email_id] = c
        except ProviderUnavailable as exc:
            print(f"  provider unavailable after {len(out)} emails: {exc}")
            break
        print(f"  {min(i + size, len(rows))}/{len(rows)}", end="\r", flush=True)
    return out, time.monotonic() - started


def score(rows: list[dict], preds: dict) -> dict:
    per = defaultdict(lambda: defaultdict(int))
    confusion = defaultdict(int)
    for row in rows:
        lang = row["language"]
        pred = preds.get(row["email_id"])
        if pred is None:
            per[lang]["missing"] += 1
            continue
        per[lang]["n"] += 1

        got, want = pred.bucket, row["bucket"]
        per[lang]["bucket_ok"] += got == want
        confusion[(want, got)] += 1

        want_tasks = json.loads(row["tasks"] or "[]")
        got_tasks = list(pred.tasks or [])
        if want_tasks:
            per[lang]["task_pos"] += 1
            per[lang]["task_pos_ok"] += bool(got_tasks)
        else:
            # The half that matters most: no task means no task.
            per[lang]["task_neg"] += 1
            per[lang]["task_neg_ok"] += not got_tasks

        want_d = row["deadline"]
        got_d = pred.deadline or next((t.due_date for t in got_tasks if t.due_date), None)
        if want_d:
            per[lang]["date_pos"] += 1
            per[lang]["date_pos_ok"] += got_d == want_d
        else:
            per[lang]["date_neg"] += 1
            per[lang]["date_neg_ok"] += not got_d
    return {"per": per, "confusion": confusion}


def pct(ok, n):
    return f"{100.0 * ok / n:5.1f}%" if n else "    --"


def report(name: str, rows: list[dict], preds: dict, secs: float) -> None:
    res = score(rows, preds)
    print(f"\n=== {name} — {len(preds)}/{len(rows)} answered in {secs:.1f}s "
          f"({secs / max(1, len(preds)):.1f}s per email) ===")
    header = f"{'':<8}{'n':>5}{'bucket':>9}{'task+':>9}{'task-':>9}{'date+':>9}{'date-':>9}"
    print(header)
    for lang in sorted(res["per"]):
        d = res["per"][lang]
        print(f"{lang:<8}{d['n']:>5}"
              f"{pct(d['bucket_ok'], d['n']):>9}"
              f"{pct(d['task_pos_ok'], d['task_pos']):>9}"
              f"{pct(d['task_neg_ok'], d['task_neg']):>9}"
              f"{pct(d['date_pos_ok'], d['date_pos']):>9}"
              f"{pct(d['date_neg_ok'], d['date_neg']):>9}")
        if d.get("missing"):
            print(f"{'':<8}{d['missing']} emails got no answer at all")
    print(f"{DIM}task- and date- are the negative cases: no task invented, no date "
          f"invented. Over-extraction is this app's documented failure mode.{RESET}")
    worst = sorted(((v, k) for k, v in res["confusion"].items() if k[0] != k[1]),
                   reverse=True)[:3]
    if worst:
        print("  most common mistakes: " +
              ", ".join(f"{w}->{g} ({n})" for n, (w, g) in worst))


DIM, RESET = "\033[2m", "\033[0m"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="", help="none | ollama | (blank = configured)")
    ap.add_argument("--model", default="")
    ap.add_argument("--host", default="")
    # `--floor/--no-floor` in one string is click's syntax, not argparse's: it
    # defines a single option literally named "--floor/--no-floor" and leaves
    # --no-floor unrecognised. The floor is on by default because a model
    # number with nothing to beat is not a measurement.
    ap.add_argument("--no-floor", dest="floor", action="store_false", default=True,
                    help="skip the structural baseline (it is the thing every "
                         "model number is meaningless without)")
    args = ap.parse_args()

    rows = labelled()
    if len(rows) < 20:
        print(f"Only {len(rows)} labelled emails. Below about 20 the per-language "
              f"numbers are noise reported to one decimal place.\n"
              f"Run scripts/build_eval_set.py then scripts/label_eval_set.py first.")
        return 1

    by_lang = defaultdict(int)
    for r in rows:
        by_lang[r["language"]] += 1
    print(f"{len(rows)} labelled emails: " +
          ", ".join(f"{k}={v}" for k, v in sorted(by_lang.items())))

    if args.floor:
        preds, secs = asyncio.run(predict(rows, "none", "", ""))
        report("none (structural regex — the floor)", rows, preds, secs)

    name = args.provider or db.get_setting("llm_provider", "none")
    if name != "none":
        preds, secs = asyncio.run(predict(rows, name, args.model, args.host))
        label = f"{name}:{args.model or db.get_setting('ollama_model','')}"
        report(label, rows, preds, secs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
