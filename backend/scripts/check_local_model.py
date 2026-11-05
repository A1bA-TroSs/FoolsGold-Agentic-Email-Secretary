#!/usr/bin/env python3
"""Does the local model actually work, and does priority actually land?

Run this on the machine that has Ollama. It is the one check that cannot be run
anywhere else: the container has no way to download or usefully run a 9B, so
`tests/test_ollama_provider.py` verifies the protocol against a faithful stub
and this verifies the judgement against a real model.

    cd backend && .venv/bin/python scripts/check_local_model.py
    cd backend && .venv/bin/python scripts/check_local_model.py --model gemma3:4b

Exits 0 only when every claim passes. A claim that raises is reported as a
failed claim, not as a dead run -- an audit that can die without printing is the
silent failure it exists to catch.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, learning, priority, relevance  # noqa: E402
from app.llm.base import ProviderUnavailable  # noqa: E402
from app.llm.ollama_provider import OllamaProvider  # noqa: E402

TODAY = date.today()
SOON = (TODAY + timedelta(days=3)).isoformat()
TOPICS = ["CO-OP", "thesis defence"]

# Ground truth the model is not told. Korean and English deliberately mixed:
# a multilingual model that quietly degrades on Korean would otherwise pass.
CASES = [
    {
        "id": "c1", "want_bucket": "action", "want_task": True, "want_deadline": True,
        "subject": "Action required: confirm your CO-OP placement by " + SOON,
        "from_name": "Career Centre", "from_address": "coop@uni.edu",
        "body_text": f"Please confirm your placement in writing before {SOON}. "
                     f"Unconfirmed places are released.",
    },
    {
        "id": "c2", "want_bucket": "action", "want_task": True, "want_deadline": True,
        "subject": f"논문 심사 일정 확정 안내 - {SOON}까지 회신 바랍니다",
        "from_name": "대학원 교학팀", "from_address": "grad@uni.edu",
        "body_text": f"논문 심사 희망 일정을 {SOON}까지 회신해 주시기 바랍니다. "
                     f"회신이 없으면 임의 배정됩니다.",
    },
    {
        "id": "c3", "want_bucket": "noise", "want_task": False, "want_deadline": False,
        "subject": "Weekly round-up: 40% off boots this weekend only",
        "from_name": "ShopCo", "from_address": "noreply@shop.example",
        "body_text": "Unsubscribe at any time. Sale ends Sunday.",
    },
    {
        "id": "c4", "want_bucket": "fyi", "want_task": False, "want_deadline": False,
        "subject": "Library opening hours during the holiday",
        "from_name": "Library", "from_address": "library@uni.edu",
        "body_text": "The library will open at 10:00 instead of 08:00. No action needed.",
    },
]


def as_email(case: dict) -> dict:
    return {
        "id": case["id"], "subject": case["subject"], "from_name": case["from_name"],
        "from_address": case["from_address"], "body_text": case["body_text"],
        "body_preview": case["body_text"][:120], "to_recipients": '["me@x.com"]',
        "cc_recipients": "[]", "received_at": f"{TODAY.isoformat()}T09:00:00+00:00",
        "is_read": 0, "is_answered": 0, "is_flagged": 0, "has_attachments": 0,
        "importance": "normal", "conversation_id": None, "web_link": "",
        "folder": "INBOX", "body_html": "", "synced_at": db.now_iso(),
    }


class Report:
    def __init__(self):
        self.rows = []

    def check(self, ok, claim, detail=""):
        self.rows.append((bool(ok), claim, detail))

    def guard(self, claim):
        report = self

        class G:
            def __enter__(self_inner):
                return report

            def __exit__(self_inner, t, e, tb):
                if t is None:
                    return False
                report.check(False, claim, f"raised {t.__name__}: {e}")
                return True
        return G()

    def render(self):
        width = max(len(c) for _, c, _ in self.rows) + 2
        print("-" * (width + 8))
        for ok, claim, detail in self.rows:
            print(f"{claim:<{width}} {'PASS' if ok else 'FAIL'}")
            for line in str(detail).splitlines()[:8]:
                if line:
                    print(f"    {line}")
        print("-" * (width + 8))
        passed = sum(1 for ok, _, _ in self.rows if ok)
        print(f"claims: {passed}/{len(self.rows)}")
        return passed == len(self.rows)


async def run(model: str, host: str) -> int:
    report = Report()
    provider = OllamaProvider(model, host)

    ok, detail = await provider.check()
    report.check(ok, "1. Ollama is running and the model is pulled", detail)
    if not ok:
        report.render()
        print("\nNothing below can run without a model. Fix the above first.")
        return 1

    emails = [as_email(c) for c in CASES]
    started = time.monotonic()
    try:
        results = await provider.classify_batch(emails, TOPICS)
    except ProviderUnavailable as exc:
        report.check(False, "2. the model answered a real batch", str(exc))
        report.render()
        return 1
    elapsed = time.monotonic() - started
    by_id = {c.email_id: c for c in results}

    report.check(
        len(results) == len(CASES) and set(by_id) == {c["id"] for c in CASES},
        "2. every email in the batch came back, and only those",
        f"{len(results)} results in {elapsed:.1f}s "
        f"({elapsed / max(1, len(CASES)):.1f}s per email)",
    )
    report.check(
        all(c.bucket in {"action", "fyi", "noise"} for c in results),
        "3. every bucket is one of the three the schema allows",
        "the enum in `format` is what makes anything else impossible to emit",
    )

    # Judgement. Reported per case so a Korean-only regression is visible rather
    # than averaged away by the English cases passing.
    wrong = [f"{c['id']} ({c['subject'][:34]}): wanted {c['want_bucket']}, "
             f"got {by_id[c['id']].bucket}"
             for c in CASES if c["id"] in by_id and by_id[c["id"]].bucket != c["want_bucket"]]
    report.check(not wrong, "4. the model agrees with the obvious answer on all four",
                 "\n".join(wrong) or "2 action (1 English, 1 Korean), 1 fyi, 1 noise")

    korean = [c for c in CASES if c["id"] == "c2"][0]
    got = by_id.get("c2")
    report.check(
        got is not None and got.bucket == korean["want_bucket"],
        "5. Korean is not quietly worse than English",
        f"c2 -> {got.bucket if got else 'missing'}; "
        f"the whole mailbox is mixed, so an average would hide this",
    )

    missing_task = [c["id"] for c in CASES if c["want_task"]
                    and not (by_id.get(c["id"]) and by_id[c["id"]].tasks)]
    report.check(not missing_task, "6. a to-do is extracted where one exists",
                 f"no tasks for {missing_task}" if missing_task else
                 "; ".join(f"{c.email_id}: {c.tasks[0].title[:40]}"
                           for c in results if c.tasks))
    invented = [c.email_id for c in results
                if c.tasks and not next(x for x in CASES if x["id"] == c.email_id)["want_task"]]
    report.check(not invented, "7. no to-do is invented where none exists",
                 f"invented for {invented}" if invented else
                 "over-extraction from newsletters is the documented failure mode")

    dated = [c["id"] for c in CASES if c["want_deadline"]
             and not (by_id.get(c["id"]) and (by_id[c["id"]].deadline or
                      any(t.due_date for t in by_id[c["id"]].tasks)))]
    report.check(not dated, "8. the deadline in the text is found",
                 f"no date for {dated}" if dated else f"both dated cases resolved to {SOON}")

    # ---------------------------------------------------------------- priority
    tmp = Path(tempfile.mkdtemp())
    db.DATA_DIR, db.DB_PATH = tmp, tmp / "check.db"
    db.init_db()
    db.set_setting("user_address", "me@x.com")
    with db.connect() as conn:
        for topic in TOPICS:
            conn.execute("INSERT INTO priorities (topic, status, weight, source, created_at) "
                         "VALUES (?, 'active', 20, 'user', ?)", (topic, db.now_iso()))
        conn.commit()
    learning.seed_centroids()
    db.upsert_emails(emails)

    with report.guard("9. the pipeline stored both axes and a reason for every email"):
        from app import pipeline
        pipeline._persist(results, {e["id"]: e for e in db.get_emails([c["id"] for c in CASES])},
                          source="llm", model=f"ollama:{model}")
        with db.connect() as conn:
            stored = {r["email_id"]: dict(r) for r in
                      conn.execute("SELECT * FROM classifications").fetchall()}
        assert len(stored) == len(CASES), f"only {len(stored)} classifications stored"
        report.check(
            all(r["actionability"] is not None and r["relevance"] is not None
                and r["reason_code"] in relevance.REASON_ORDER for r in stored.values()),
            "9. the pipeline stored both axes and a reason for every email",
            "; ".join(f"{k}: A={v['actionability']} R={v['relevance']} "
                      f"({v['reason_code']}{'/' + v['reason_arg'] if v['reason_arg'] else ''})"
                      for k, v in sorted(stored.items())),
        )

    with report.guard("10. the declared priority outranks the promotion"):
        with db.connect() as conn:
            ranked = [dict(r) for r in conn.execute(
                "SELECT email_id, bucket, score FROM classifications ORDER BY score DESC")]
        assert ranked, "nothing ranked -- the assertions below would be vacuous"
        order = [r["email_id"] for r in ranked]
        noise_last = order.index("c3") == len(order) - 1
        topic_first = order[0] in ("c1", "c2")

        # The order alone is not evidence. c1 and c2 both carry a deadline, so
        # they sort to the top whether or not the priority list was consulted
        # at all -- which is exactly how this claim passed on 2026-09-19 while
        # the topic feature was contributing zero to every email in the batch.
        # The stored reason is what distinguishes "ranked first because you
        # said CO-OP matters" from "ranked first because it has a date".
        with db.connect() as conn:
            c1 = dict(conn.execute(
                "SELECT * FROM classifications WHERE email_id='c1'").fetchone())
        because_of_topic = c1["reason_code"] == "topic"
        report.check(
            noise_last and topic_first and because_of_topic,
            "10. the declared priority outranks the promotion",
            " > ".join(f"{r['email_id']}({r['bucket']},{r['score']})" for r in ranked)
            + f"; c1 ranked because of {c1['reason_code']}"
            + ("" if because_of_topic else " -- NOT the priority list, so the order above "
                                           "proves nothing about priority"),
        )

    with report.guard("11. editing the priority list re-ranks without calling the model"):
        from app import pipeline
        with db.connect() as conn:
            before = dict(conn.execute(
                "SELECT * FROM classifications WHERE email_id='c1'").fetchone())
            conn.execute("DELETE FROM priorities WHERE topic = 'CO-OP'")
            conn.commit()
        started = time.monotonic()
        pipeline.rescore_all()
        took = time.monotonic() - started
        with db.connect() as conn:
            after = dict(conn.execute(
                "SELECT * FROM classifications WHERE email_id='c1'").fetchone())
        report.check(
            after["relevance"] < before["relevance"] and took < 5.0,
            "11. editing the priority list re-ranks without calling the model",
            f"c1 relevance {before['relevance']} -> {after['relevance']} in {took:.2f}s "
            f"(no model call: rescore_all never touches the provider)",
        )

    with report.guard("12. a Korean email matches an English priority"):
        # The app promises *semantic* priority. `hashing_embed` is a bag of
        # words, so it cannot bridge languages at all -- which means today this
        # can only come from the model's own `matched` list. c2 is Korean and
        # says nothing that looks like "thesis defence" in ASCII.
        #
        # If this fails, the priority list is language-locked: a Korean user
        # gets matches only on topics they typed in Korean, and the feature is
        # not what the settings screen says it is. That is the measurement that
        # decides whether a real multilingual embedder is urgent or merely nice.
        c2 = by_id.get("c2")
        hit = bool(c2 and any("thesis" in str(m).lower() or "defence" in str(m).lower()
                              for m in (c2.matched or ())))
        report.check(
            hit,
            "12. a Korean email matches an English priority",
            f"c2 matched {list(c2.matched) if c2 else 'nothing'} against {TOPICS}"
            + ("" if hit else " -- the priority list is language-locked today"),
        )

    ok = report.render()
    if ok:
        print(f"\n{model} is wired in and priority lands. "
              f"{elapsed / max(1, len(CASES)):.1f}s per email on this machine.")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="")
    ap.add_argument("--host", default="")
    # Written into the repo by default so the result is readable from a
    # connected-folder session without anyone copying terminal output around.
    # The repo is the one place both sides can already see.
    ap.add_argument("--report", default=str(
        Path(__file__).resolve().parent.parent.parent / "verify_local_model.txt"))
    args = ap.parse_args()
    model = args.model or db.get_setting("ollama_model", "") or "qwen3.5:9b"
    buffer: list[str] = []

    class Tee:
        def write(self, text):
            buffer.append(text)
            sys.__stdout__.write(text)

        def flush(self):
            sys.__stdout__.flush()

    sys.stdout = Tee()
    try:
        code = asyncio.run(run(model, args.host))
    finally:
        sys.stdout = sys.__stdout__
    if args.report:
        try:
            Path(args.report).write_text(
                f"model: {model}\nhost: {args.host or 'default'}\n"
                f"when: {db.now_iso()}\nexit: {code}\n\n" + "".join(buffer),
                encoding="utf-8")
            print(f"\nreport written to {args.report}")
        except OSError as exc:
            print(f"\ncould not write the report: {exc}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
