"""Muting a sender, and the accounting the confirmation dialog depends on."""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from app import db


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    rows = [
        dict(id=f"m{i}", conversation_id=None, subject=f"Deal {i}", from_name="ShopCo",
             from_address="news@shop.com", to_recipients="[]", cc_recipients="[]",
             received_at="2026-08-20T09:00:00+00:00", is_read=1, is_answered=0, is_flagged=0,
             has_attachments=0, importance="normal", web_link="", folder="INBOX",
             body_preview="", body_text="", body_html="", synced_at=db.now_iso())
        for i in range(4)
    ]
    rows.append(dict(id="keep", conversation_id=None, subject="Real mail", from_name="Prof. Lee",
                     from_address="lee@uni.edu", to_recipients="[]", cc_recipients="[]",
                     received_at="2026-08-25T09:00:00+00:00", is_read=0, is_answered=0, is_flagged=0,
                     has_attachments=0, importance="normal", web_link="", folder="INBOX",
                     body_preview="", body_text="", body_html="", synced_at=db.now_iso()))
    db.upsert_emails(rows)
    return tmp_path


def test_mute_and_unmute_round_trip(store):
    assert db.muted_senders() == set()
    db.mute_sender("News@Shop.com")
    assert db.muted_senders() == {"news@shop.com"}, "addresses normalise to lowercase"
    db.unmute_sender("news@shop.com")
    assert db.muted_senders() == set()


def test_muting_twice_is_not_an_error(store):
    db.mute_sender("news@shop.com")
    db.mute_sender("news@shop.com")
    assert len(db.muted_senders()) == 1


def test_impact_count_is_what_the_dialog_promises(store):
    """The confirmation says 'this covers N emails'; N has to be true."""
    assert db.count_from_sender("news@shop.com") == 4
    assert db.count_from_sender("NEWS@SHOP.COM") == 4
    assert db.count_from_sender("nobody@nowhere.com") == 0


def test_muted_rows_report_volume_and_a_display_name(store):
    db.mute_sender("news@shop.com")
    rows = db.muted_sender_rows()
    assert len(rows) == 1
    assert rows[0]["address"] == "news@shop.com"
    assert rows[0]["message_count"] == 4
    assert rows[0]["display_name"] == "ShopCo"


def test_muting_an_address_with_no_mail_yet_still_works(store):
    """You can mute a sender from a single first message."""
    db.mute_sender("brand-new@vendor.com")
    rows = {r["address"]: r for r in db.muted_sender_rows()}
    assert rows["brand-new@vendor.com"]["message_count"] == 0


def test_blank_addresses_are_ignored(store):
    db.mute_sender("")
    db.mute_sender("   ")
    assert db.muted_senders() == set()
