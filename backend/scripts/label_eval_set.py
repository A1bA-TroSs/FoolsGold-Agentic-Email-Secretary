#!/usr/bin/env python3
"""Label the evaluation set by hand, one email at a time, resumably.

    cd backend && .venv/bin/python scripts/label_eval_set.py

Single keystrokes, because 120 emails at three keystrokes each is five minutes
of typing and 120 emails at a sentence each is an evening. Progress is saved
after every email, so this can be done in four sittings of thirty.

Two design choices worth defending:

**It never shows you the model's answer.** Anchoring is not a small effect --
shown a suggested label, people agree with it most of the time, and the set
would then measure agreement-with-the-model rather than correctness. The
sampler already used the stored classification to spread the sample; that is
the last point at which it is allowed to touch anything.

**"No task" and "no deadline" are answers, not skips.** The medical-distillation
work that this set's design borrows from (npj Digital Medicine 2025) built
roughly a third of its 634k synthetic pairs as deliberately unanswerable cases,
and reported that as the choice that taught the student to abstain. Over-
extraction from newsletters is this app's documented failure mode, so the
negative cases are the ones that will earn their keep.
"""
from __future__ import annotations

import json
import sys
import termios
import tty
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def key() -> str:
    """One keystroke, no Enter. Falls back to a line read where that is not
    possible (a pipe, an IDE console) rather than crashing."""
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
    except Exception:
        return (sys.stdin.readline() or "q").strip()[:1].lower()
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if ch in ("\x03", "\x04"):
        raise KeyboardInterrupt
    return ch.lower()


def ask(prompt: str, options: dict[str, str]) -> str | None:
    legend = "  ".join(f"[{k}] {v}" for k, v in options.items())
    while True:
        print(f"{prompt}  {DIM}{legend}{RESET}", end="  ", flush=True)
        ch = key()
        if ch == "q":
            print("quit")
            return None
        if ch in options:
            print(options[ch])
            return ch
        print(f"{DIM}?{RESET}")


def line(text: str) -> str:
    """Read a line with the terminal back in its normal mode."""
    return input(text).strip()


def main() -> int:
    with db.connect() as conn:
        todo = [dict(r) for r in conn.execute(
            "SELECT l.email_id, l.language, l.stratum, e.subject, e.from_name, "
            "       e.from_address, e.body_text, e.received_at "
            "FROM eval_labels l JOIN emails e ON e.id = l.email_id "
            "WHERE l.bucket IS NULL ORDER BY l.email_id")]
        done = conn.execute(
            "SELECT COUNT(*) FROM eval_labels WHERE bucket IS NOT NULL").fetchone()[0]

    if not todo:
        if done:
            print(f"Nothing left to label. {done} labelled.\n"
                  f"Next: .venv/bin/python scripts/eval_extraction.py")
        else:
            # "Nothing left to label" on an empty set reads as "you have
            # finished", which is the opposite of true and sends the reader
            # looking for a set that was never built.
            print("No emails have been sampled yet, so there is nothing to label.\n"
                  "Run: .venv/bin/python scripts/build_eval_set.py --n 120")
        return 0

    print(f"{done} labelled, {len(todo)} to go. [q] saves and quits at any point.\n")
    for n, row in enumerate(todo, 1):
        body = (row["body_text"] or "").strip().replace("\r", "")
        print("=" * 76)
        print(f"{DIM}{n}/{len(todo)} · {row['stratum']} · {row['received_at'][:10]}{RESET}")
        print(f"{BOLD}{row['subject']}{RESET}")
        print(f"{DIM}from {row['from_name'] or ''} <{row['from_address']}>{RESET}\n")
        print(body[:900] + ("…" if len(body) > 900 else ""))
        print()

        b = ask("bucket?", {"a": "action", "f": "fyi", "n": "noise"})
        if b is None:
            break
        bucket = {"a": "action", "f": "fyi", "n": "noise"}[b]

        addressed = ask("addressed to you personally?", {"y": "yes", "n": "no"})
        if addressed is None:
            break

        deadline = None
        d = ask("is there a date in the text you are expected to act by?",
                {"y": "yes", "n": "no"})
        if d is None:
            break
        if d == "y":
            deadline = line("    the date, YYYY-MM-DD (blank = none after all): ") or None

        tasks: list[dict] = []
        t = ask("does this give YOU something to do?", {"y": "yes", "n": "no"})
        if t is None:
            break
        if t == "y":
            while len(tasks) < 5:
                title = line(f"    task {len(tasks)+1} (verb first, blank to stop): ")
                if not title:
                    break
                due = line("       due YYYY-MM-DD (blank = the email never says): ") or None
                tasks.append({"title": title, "due": due})

        with db.connect() as conn:
            conn.execute(
                "UPDATE eval_labels SET bucket=?, deadline=?, tasks=?, addressed=?, "
                "labelled_at=? WHERE email_id=?",
                (bucket, deadline, json.dumps(tasks, ensure_ascii=False),
                 1 if addressed == "y" else 0, db.now_iso(), row["email_id"]))
            conn.commit()
        print()

    with db.connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM eval_labels WHERE bucket IS NOT NULL").fetchone()[0]
        by_lang = conn.execute(
            "SELECT language, bucket, COUNT(*) n FROM eval_labels "
            "WHERE bucket IS NOT NULL GROUP BY language, bucket").fetchall()
    print("=" * 76)
    print(f"{total} labelled.")
    for r in by_lang:
        print(f"   {r['language']:<6} {r['bucket']:<7} {r['n']}")
    print("\nNext: .venv/bin/python scripts/eval_extraction.py")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nstopped; everything answered so far is saved.")
        raise SystemExit(130)
