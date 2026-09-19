"""The evaluation set's own plumbing.

A measurement harness that is wrong measures confidently, so these pin the two
things that would silently corrupt every number Phase 1 produces: that the
sample is spread across languages rather than following the mailbox's own
imbalance, and that a label is never seeded from the classification it is
supposed to judge.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app import db

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _email(i, ko):
    return dict(id=f"m{i}", conversation_id=None,
                subject="논문 심사 일정 안내" if ko else "Confirm your placement",
                from_name="X", from_address=f"s{i % 7}@uni.edu",
                to_recipients='["me@x.com"]', cc_recipients="[]",
                received_at="2026-09-17T09:00:00+00:00", is_read=0, is_answered=0,
                is_flagged=0, has_attachments=0, importance="normal", web_link="",
                folder="INBOX", body_preview="x",
                body_text="회신 바랍니다" if ko else "Please confirm.",
                body_html="", synced_at=db.now_iso())


@pytest.fixture()
def mailbox(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    # Deliberately lopsided: 90 English, 10 Korean, which is roughly what a
    # mixed mailbox looks like and exactly what a uniform sample would flatten.
    rows = [_email(i, ko=i >= 90) for i in range(100)]
    db.upsert_emails(rows)
    with db.connect() as conn:
        for i, r in enumerate(rows):
            conn.execute("INSERT INTO classifications (email_id, bucket, score, source) "
                         "VALUES (?, ?, ?, 'llm')",
                         (r["id"], "noise" if i % 5 else "action", 10))
        conn.commit()
    return tmp_path


def test_the_sample_reaches_the_minority_language(mailbox, monkeypatch, capsys):
    """10% of the mailbox is Korean. A uniform sample of 20 would be expected
    to contain two, and could easily contain none -- which would leave the one
    question this set exists to answer unanswerable.

    Measured on the real mailbox afterwards, the true figure was 3%: 37 emails
    of 1,218 contained any Hangul, and only 8 were Korean-dominant. The fixture
    here is ten times kinder than reality, which is the right direction for a
    test to be wrong in."""
    build = _load("build_eval_set")
    monkeypatch.setattr("sys.argv", ["build_eval_set.py", "--n", "20"])
    assert build.main() == 0
    with db.connect() as conn:
        langs = dict(conn.execute(
            "SELECT language, COUNT(*) FROM eval_labels GROUP BY language").fetchall())
    assert langs.get("ko", 0) >= 5, langs


def test_sampling_never_writes_a_label(mailbox, monkeypatch):
    """The stored classification spreads the sample and must not become ground
    truth -- a set labelled by the model measures agreement, not correctness."""
    build = _load("build_eval_set")
    monkeypatch.setattr("sys.argv", ["build_eval_set.py", "--n", "20"])
    build.main()
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM eval_labels")]
    assert rows
    assert all(r["bucket"] is None for r in rows)
    assert all(r["labelled_at"] is None for r in rows)


def test_resampling_keeps_work_already_done(mailbox, monkeypatch):
    """Labelling 120 emails takes more than one sitting. Re-running the sampler
    must not throw away the sittings that already happened."""
    build = _load("build_eval_set")
    monkeypatch.setattr("sys.argv", ["build_eval_set.py", "--n", "20"])
    build.main()
    with db.connect() as conn:
        first = conn.execute("SELECT email_id FROM eval_labels LIMIT 1").fetchone()[0]
        conn.execute("UPDATE eval_labels SET bucket='action', tasks='[]', labelled_at=? "
                     "WHERE email_id=?", (db.now_iso(), first))
        conn.commit()
    monkeypatch.setattr("sys.argv", ["build_eval_set.py", "--n", "30", "--reset"])
    build.main()
    with db.connect() as conn:
        kept = conn.execute("SELECT bucket FROM eval_labels WHERE email_id=?",
                            (first,)).fetchone()[0]
    assert kept == "action"


def test_the_scorer_separates_languages_and_counts_negatives(mailbox, monkeypatch):
    """An average over a mixed mailbox hides a Korean-only regression, and a
    task score that ignores the no-task cases hides over-extraction."""
    db.set_setting("user_address", "me@x.com")
    with db.connect() as conn:
        for i in range(40):
            conn.execute(
                "INSERT INTO eval_labels (email_id,bucket,deadline,tasks,addressed,"
                "language,stratum,labelled_at) VALUES (?,?,?,?,1,?,?,?)",
                (f"m{i}", "action" if i % 2 else "noise", None,
                 json.dumps([{"title": "Do it", "due": None}] if i % 2 else []),
                 "ko" if i >= 30 else "en", "x", db.now_iso()))
        conn.commit()
    ev = _load("eval_extraction")
    rows = ev.labelled()
    assert len(rows) == 40
    from app import pipeline
    preds = {c.email_id: c for c in pipeline._structural_classifications(
        [dict(r, id=r["email_id"]) for r in rows], "me@x.com")}
    res = ev.score(rows, preds)
    assert set(res["per"]) == {"ko", "en"}, "languages must not be averaged together"
    for lang in ("ko", "en"):
        d = res["per"][lang]
        # The negative cases are counted at all -- the bug this guards against
        # is a scorer that only ever looks at emails that DO carry a task.
        assert d["task_neg"] > 0


def test_hangul_inside_an_english_email_is_not_counted_as_english(mailbox):
    """The rule was "which script wins", and on the real mailbox that labelled
    29 of the 37 Hangul-carrying emails as English -- so the sample reached one
    Korean email out of 120.

    What matters is not which language an email is *in*. It is whether the
    model handles Hangul content at all, and a Korean deadline sentence inside
    an English departmental notice is exactly the case that breaks."""
    build = _load("build_eval_set")
    mostly_english = {
        "subject": "Reminder: Graduate School submission window",
        "body_text": "The Graduate School has confirmed the schedule. "
                     "제출 마감은 9월 22일까지입니다. Please submit through the portal "
                     "and contact the office with any questions about the process.",
    }
    assert build.language_of(mostly_english) == "mixed"
    assert build.language_of({"subject": "Weekly digest", "body_text": "No action needed."}) == "en"
    assert build.language_of({"subject": "논문 심사 안내", "body_text": "회신 바랍니다."}) == "ko"
