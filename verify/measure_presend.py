#!/usr/bin/env python3
"""How loud are the deterministic checks on mail the user already sent?

Every message in the Sent folder was sent as-is, by a person who was happy with
it. So **every flag this script reports is a false alarm by construction**,
except where reading it shows a real mistake that went out. That is the whole
measurement: false alarms per 1,000 words, by rule.

The number matters because a checker whose warnings are mostly wrong gets
ignored wholesale -- 90% ignored it even on a bank site, when most of the
warnings were false positives (Sunshine et al., USENIX Security 2009).

Run:  python3 verify/measure_presend.py [--db PATH] [--samples 3]
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.assist import checks                                   # noqa: E402
from app.assist import harper_bridge                            # noqa: E402
from app.assist import lexicon                                  # noqa: E402

SENT_NAMES = {"sent", "sent items", "sent messages", "보낸 편지함", "보낸편지함",
              "已发送", "送信済み", "送信済みメール"}
_WORD = re.compile(r"[A-Za-z0-9']+|[가-힣]+")


def words(text: str) -> int:
    """Hangul counted in eojeol (space-separated chunks), English in words."""
    return len(_WORD.findall(text or ""))


def _familiar(db_path: str, min_count: int) -> frozenset[str]:
    """The mailbox's own vocabulary, read straight from the database file so the
    measurement does not need the app's settings or environment."""
    conn = sqlite3.connect(db_path)
    seen: dict[str, int] = {}
    for subject, body in conn.execute("SELECT subject, body_text FROM emails"):
        lexicon._count(subject or "", seen)
        lexicon._count(body or "", seen)
    return frozenset(w for w, n in seen.items() if n >= min_count)


def _korean_proxy(conn, samples: int) -> None:
    """The Sent folder may hold almost no Korean -- it holds four messages on
    the reference mailbox -- so a zero there means nothing. Received Korean
    mail was written by real people who sent it as-is, which makes it a usable
    (imperfect) false-alarm corpus for a check about internal consistency."""
    rows = [r for r in conn.execute("SELECT subject, body_text, folder FROM emails")
            if unicodedata.normalize("NFC", (r["folder"] or "")).strip().lower() not in SENT_NAMES]
    korean, flags, total_words, shown = 0, 0, 0, 0
    for row in rows:
        own = checks.own_text(row["body_text"] or "")
        if not re.search(r"[\uac00-\ud7a3]", own):
            continue
        korean += 1
        total_words += words(own)
        found = checks.check_honorific(own)
        flags += len(found)
        if found and shown < samples:
            shown += 1
            print(f"    [{(row['subject'] or '')[:28]}] {found[0].evidence[:70]}")
    rate = (flags / total_words * 1000) if total_words else 0.0
    print(f"korean received messages: {korean}, words {total_words}, "
          f"honorific flags {flags} ({rate:.2f} per 1k words)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.expanduser("~/.foolsgold/foolsgold.db"))
    ap.add_argument("--samples", type=int, default=3, help="evidence lines to print per rule")
    ap.add_argument("--no-english", action="store_true", help="skip Harper")
    ap.add_argument("--dialect", default="", help="american | british | canadian | australian")
    ap.add_argument("--min-count", type=int, default=lexicon.MIN_COUNT,
                    help="how often a token must appear in this mailbox to count as a word")
    ap.add_argument("--korean-proxy", action="store_true",
                    help="also run the honorific check over received Korean mail, "
                         "because the Sent folder may hold too little Korean to measure")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = [r for r in conn.execute(
        "SELECT subject, body_text, has_attachments, folder FROM emails")
        if unicodedata.normalize("NFC", (r["folder"] or "")).strip().lower() in SENT_NAMES]
    if not rows:
        print("no sent mail found -- is this the right database?")
        return 2

    familiar = _familiar(args.db, args.min_count)
    print(f"mailbox vocabulary: {len(familiar)} tokens seen {args.min_count}+ times")

    def linter_fn(text: str):
        return harper_bridge.lint(text, familiar=familiar, dialect=args.dialect)

    linter = None if args.no_english else linter_fn
    if linter and not harper_bridge.available():
        print("! Harper not available; English checks skipped")
        linter = None

    total_words = 0
    flagged_messages = 0
    per_kind: Counter[str] = Counter()
    messages_per_kind: Counter[str] = Counter()
    samples: dict[str, list[str]] = defaultdict(list)

    for row in rows:
        body = row["body_text"] or ""
        own = checks.own_text(body)
        total_words += words(own)
        found = checks.check(row["subject"] or "", body,
                             has_attachment=bool(row["has_attachments"]),
                             linter=linter)
        if found:
            flagged_messages += 1
        for kind in {i.kind for i in found}:
            messages_per_kind[kind] += 1
        for issue in found:
            per_kind[issue.kind] += 1
            if len(samples[issue.kind]) < args.samples:
                samples[issue.kind].append(
                    f"[{row['subject'][:28] if row['subject'] else '(no subject)'}] "
                    f"{issue.evidence[:70]}")

    # What was actually exercised. A rule that never had the chance to fire has
    # not been measured, and a zero next to it means nothing -- this project has
    # shipped a green check that could not fail before.
    promises = sum(1 for r in rows if checks._PROMISE.search(checks.own_text(r["body_text"] or "")))
    promises_ok = sum(1 for r in rows
                      if checks._PROMISE.search(checks.own_text(r["body_text"] or ""))
                      and r["has_attachments"])
    korean = sum(1 for r in rows
                 if re.search(r"[\uac00-\ud7a3]", checks.own_text(r["body_text"] or "")))
    print(f"sent messages : {len(rows)}")
    print(f"  of which mention an attachment : {promises}"
          f"  (correctly silent because one was attached: {promises_ok})")
    print(f"  of which contain any Korean    : {korean}")
    print(f"words (own text, quotes and signatures removed) : {total_words}")
    print(f"messages with at least one flag : {flagged_messages}"
          f"  ({flagged_messages / len(rows):.0%})")
    print()
    print(f"{'rule':<20}{'flags':>7}{'messages':>10}{'per 1k words':>14}")
    print("-" * 51)
    for kind in ("subject_missing", "attachment_missing", "honorific_mixed", "spelling_en"):
        count = per_kind.get(kind, 0)
        rate = (count / total_words * 1000) if total_words else 0.0
        print(f"{kind:<20}{count:>7}{messages_per_kind.get(kind, 0):>10}{rate:>14.2f}")
    total = sum(per_kind.values())
    print("-" * 51)
    print(f"{'ALL':<20}{total:>7}{flagged_messages:>10}"
          f"{(total / total_words * 1000 if total_words else 0):>14.2f}")
    print()
    for kind, lines in samples.items():
        print(f"— {kind}")
        for line in lines:
            print(f"    {line}")
    if args.korean_proxy:
        print()
        print("— honorific check over received Korean mail (proxy corpus)")
        _korean_proxy(conn, args.samples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
