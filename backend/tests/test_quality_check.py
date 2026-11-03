"""A5: the tester quality check, in the app instead of three terminal scripts.

What would silently corrupt the numbers testers send back, and is pinned here:
the label screen seeing the app's answer, a score computed differently from
what it claims, Apple's Priority mark read from the wrong row, and a score
card that carries a single word of anyone's mail."""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db, quality

SECRET = "QQSECRET"


def _email(i, ko, path=""):
    return dict(id=f"m{i}", conversation_id=None,
                subject=f"{SECRET} 논문 심사 일정 안내 및 회신 요청" if ko else f"{SECRET} Confirm your placement",
                from_name=f"{SECRET} Kim", from_address=f"{SECRET.lower()}{i}@uni.edu",
                to_recipients='["me@x.com"]', cc_recipients="[]",
                received_at="2026-09-17T09:00:00+00:00", is_read=0, is_answered=0,
                is_flagged=0, has_attachments=0, importance="normal", web_link="",
                folder="INBOX", body_preview="x",
                body_text=f"{SECRET} 금요일까지 회신 바랍니다 감사합니다" if ko else f"{SECRET} Please confirm.",
                body_html="", synced_at=db.now_iso(), source_path=path)


@pytest.fixture()
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.set_setting("user_address", "me@x.com")
    root = tmp_path / "Mail" / "V10"
    msgs = root / "acct" / "INBOX.mbox" / "Messages"
    msgs.mkdir(parents=True)
    (root / "MailData").mkdir()
    db.set_setting("applemail_root", str(root))
    rows = [_email(i, ko=i >= 30, path=str(msgs / f"{100 + i}.emlx")) for i in range(40)]
    db.upsert_emails(rows)
    with db.connect() as conn:
        for i, r in enumerate(rows):
            conn.execute("INSERT INTO classifications (email_id, bucket, score, source, rationale, model) "
                         "VALUES (?, ?, 10, 'llm', ?, 'qwen3.5:9b')",
                         (r["id"], "action" if i % 2 == 0 else "noise", f"{SECRET} because"))
        conn.commit()
    return root


@pytest.fixture()
def client(box):
    from app.main import app
    with TestClient(app) as c:
        yield c


def envelope(root, urgent: dict[int, int], with_column=True):
    con = sqlite3.connect(root / "MailData" / "Envelope Index")
    col = ", urgent INTEGER" if with_column else ""
    con.execute(f"CREATE TABLE message_global_data (ROWID INTEGER PRIMARY KEY{col})")
    con.execute("CREATE TABLE messages (ROWID INTEGER PRIMARY KEY, global_message_id INTEGER)")
    for rowid, u in urgent.items():
        if with_column:
            con.execute("INSERT INTO message_global_data VALUES (?, ?)", (rowid + 9000, u))
        else:
            con.execute("INSERT INTO message_global_data VALUES (?)", (rowid + 9000,))
        con.execute("INSERT INTO messages VALUES (?, ?)", (rowid, rowid + 9000))
    con.commit()
    con.close()


def label_all(client, plan):
    for eid, bucket in plan.items():
        r = client.post("/api/quality/label", json={"email_id": eid, "bucket": bucket,
                                                    "addressed": True, "deadline": None})
        assert r.status_code == 200, r.text


def test_sampling_spreads_languages_and_writes_no_label(client):
    r = client.post("/api/quality/sample", json={"n": 16})
    assert r.status_code == 200
    with db.connect() as conn:
        rows = [dict(x) for x in conn.execute("SELECT * FROM eval_labels")]
    assert len(rows) == 16
    assert all(x["bucket"] is None for x in rows)
    assert sum(x["language"] == "ko" for x in rows) >= 5   # 10 of 40 are Korean


def test_resampling_keeps_labels_and_tops_up_to_the_target(client):
    client.post("/api/quality/sample", json={"n": 10})
    first = client.get("/api/quality/next").json()["item"]["email_id"]
    label_all(client, {first: "action"})
    client.post("/api/quality/sample", json={"n": 10})
    assert quality.progress() == {"total": 10, "labelled": 1}
    client.post("/api/quality/sample", json={"n": 15})
    with db.connect() as conn:
        assert conn.execute("SELECT bucket FROM eval_labels WHERE email_id=?", (first,)).fetchone()[0] == "action"
    assert quality.progress()["total"] == 15


def test_the_label_screen_never_sees_the_apps_answer(client):
    client.post("/api/quality/sample", json={"n": 5})
    body = client.get("/api/quality/next").json()
    text = json.dumps(body, ensure_ascii=False)
    assert body["item"]["subject"]
    for leak in ("bucket", "score", "rationale", "because", "qwen"):
        assert leak not in text


@pytest.mark.parametrize("label, status", [
    ({"bucket": "urgent"}, 400),
    ({"bucket": "action", "deadline": "next friday"}, 400),
    ({"bucket": "action", "email_id": "not-sampled"}, 404),
])
def test_bad_labels_are_refused(client, label, status):
    client.post("/api/quality/sample", json={"n": 5})
    eid = client.get("/api/quality/next").json()["item"]["email_id"]
    r = client.post("/api/quality/label", json={"email_id": eid, "addressed": False, **label})
    assert r.status_code == status


def test_scores_match_hand_counts_per_language(client):
    client.post("/api/quality/sample", json={"n": 40})
    # Tester: m0..m9 action, the rest fyi. App: even ids action.
    plan = {f"m{i}": ("action" if i < 10 or 30 <= i < 34 else "fyi") for i in range(40)}
    label_all(client, plan)
    s = client.get("/api/quality/score").json()
    en, ko = s["languages"]["en"], s["languages"]["ko"]
    assert (en["n"], en["acted"]) == (30, 10)
    assert en["fg_act_recall"] == 0.5          # m0,2,4,6,8 of m0..m9
    assert en["fg_act_precision"] == round(5 / 15, 4)   # 15 even ids in m0..m29
    assert en["bucket_agreement"] == round(5 / 30, 4)   # fyi never equals noise
    assert (ko["n"], ko["acted"]) == (10, 4)
    assert ko["fg_act_recall"] == 0.5          # m30, m32 of m30..m33
    assert en["floor_act_recall"] is not None
    assert s["apple"]["measured"] is False and en["apple_act_recall"] is None


def test_apple_priority_is_read_from_a_copy_and_matched_by_file_number(client, box):
    client.post("/api/quality/sample", json={"n": 40})
    plan = {f"m{i}": ("action" if i < 10 else "fyi") for i in range(40)}
    label_all(client, plan)
    # Priority on m0, m1, m2 (rows 100-102) and on a non-action mail m20.
    envelope(box, {100 + i: int(i in (0, 1, 2, 20)) for i in range(40)})
    index = box / "MailData" / "Envelope Index"
    before = (index.stat().st_mtime_ns, index.read_bytes())
    s = client.get("/api/quality/score").json()
    assert s["apple"]["measured"] is True and s["apple"]["matched"] == 40
    assert s["languages"]["en"]["apple_act_recall"] == 0.3
    assert s["languages"]["en"]["apple_precision"] == 0.75
    assert (index.stat().st_mtime_ns, index.read_bytes()) == before


def test_a_mail_version_without_the_column_is_not_measured(client, box):
    client.post("/api/quality/sample", json={"n": 40})
    label_all(client, {f"m{i}": "action" for i in range(40)})
    envelope(box, {100 + i: 1 for i in range(40)}, with_column=False)
    s = client.get("/api/quality/score").json()
    assert s["apple"] == {"measured": False, "reason": "no_priority_column", "matched": 0}


def test_a_blocked_mail_folder_is_not_measured(client, monkeypatch):
    from app.sources import applemail
    def blocked(_=None):
        raise PermissionError("TCC")
    monkeypatch.setattr(applemail, "find_mail_root", blocked)
    client.post("/api/quality/sample", json={"n": 40})
    label_all(client, {f"m{i}": "action" for i in range(40)})
    apple = client.get("/api/quality/score").json()["apple"]
    assert apple["measured"] is False and apple["reason"] == "no_index"


def test_the_score_card_carries_numbers_only(client, box):
    client.post("/api/quality/sample", json={"n": 40})
    label_all(client, {f"m{i}": "action" for i in range(40)})
    envelope(box, {100 + i: 1 for i in range(40)})
    r = client.get("/api/quality/export")
    assert r.status_code == 200
    text = r.text
    for leak in (SECRET, SECRET.lower(), "Kim", "uni.edu", "2026-09-17", "emlx",
                 str(box), '"m1"', "because", "논문"):
        assert leak not in text, leak
    card = r.json()
    assert card["format"] == "foolsgold-quality-1"
    assert card["labelled"] == 40 and card["apple"]["measured"] is True
    assert set(card["languages"]) <= {"ko", "en", "mixed"}
    for d in card["languages"].values():
        assert all(v is None or isinstance(v, (int, float)) for v in d.values())
    # Same install, same id; never derived from the mail.
    assert client.get("/api/quality/export").json()["install_id"] == card["install_id"]
