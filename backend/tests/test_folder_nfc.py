"""Korean folder names arrive decomposed from macOS, and comparisons must not care.

Found by measurement, not by review: every `folder` value in the real database
on the reference Mac is NFD ("보낸 편지함" as decomposed jamo), because the
names come from directory names on an Apple filesystem. `SENT_NAMES` is
written in NFC, so `leaf in SENT_NAMES` matched nothing and automatic sending
knew only the configured address -- silently, with no error anywhere.
"""
from __future__ import annotations

import unicodedata

import pytest

from app import db
from app.compose.identity import known_identities

NFD_SENT = unicodedata.normalize("NFD", "보낸 편지함")
MAIN, OTHER = "danny@uni.edu", "danny@dept.uni.edu"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    db.init_db()
    db.set_setting("user_address", MAIN)
    db.upsert_emails([dict(
        id="s1", conversation_id=None, subject="x", from_name="", from_address=OTHER,
        to_recipients="[]", cc_recipients="[]", received_at="2026-09-01T00:00:00+00:00",
        is_read=1, is_answered=0, is_flagged=0, has_attachments=0, importance="normal",
        web_link="", folder=NFD_SENT, body_preview="", body_text="", body_html="",
        source_path="", synced_at=db.now_iso())])
    return tmp_path


def test_the_test_data_is_really_decomposed():
    assert NFD_SENT != "보낸 편지함"
    assert unicodedata.normalize("NFC", NFD_SENT) == "보낸 편지함"


def test_a_decomposed_korean_sent_folder_still_yields_its_identity(store):
    assert known_identities() == [MAIN, OTHER]


def test_the_apple_mail_source_writes_composed_folder_names():
    from app.sources import applemail
    import inspect
    source = inspect.getsource(applemail)
    assert 'normalize("NFC", "/".join(chain))' in source, \
        "normalise at the filesystem boundary, not only at comparison sites"
