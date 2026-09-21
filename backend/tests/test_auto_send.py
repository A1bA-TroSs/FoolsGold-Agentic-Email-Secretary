"""Sending with nothing configured: A -> B from the address A was delivered to.

Real sockets on both sides (fake_smtp, fake_imap), real compose route, real
registry. The only thing seeded is where the servers are -- which in the app
is found by discovery, and is tested on its own in test_autoconfig.py.
"""
from __future__ import annotations

import email
import email.policy
import functools
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from fake_imap import FakeIMAP                                    # noqa: E402
from fake_smtp import FakeSMTP                                    # noqa: E402

from app import db                                                # noqa: E402
from app.compose import Mailbox, ParentMessage                    # noqa: E402
from app.compose.identity import pick_sender                      # noqa: E402
from app.main import app                                          # noqa: E402
from app.transports import accounts, auto_transport, imap_folders  # noqa: E402
from app.transports.autoconfig import ServerConfig                # noqa: E402

MAIN, SECOND = "danny@example.edu", "danny.park@alumni.example.org"
PASSWORD = "app-password"

PARENT = ParentMessage(
    message_id="<talk@careers.example.edu>", references="<root@careers.example.edu>",
    subject="Recruitment Talks", sender=(Mailbox("careers@example.edu", "Career Center"),),
    to=(Mailbox("all-students@lists.example.org"),),
    delivered_to=(Mailbox(SECOND),),
    body_text="Register by the 23rd.")


# ------------------------------------------------------------------ identity

def test_a_list_reply_comes_from_the_address_it_was_delivered_to():
    """To: names the list; Delivered-To: names you. The reply belongs to the
    second -- that is what 'reply from the account I'm reading' means."""
    got = pick_sender(PARENT, [MAIN, SECOND], name="Danny")
    assert got.address == SECOND


def test_delivery_headers_outrank_to_and_cc():
    p = ParentMessage(to=(Mailbox(MAIN),), delivered_to=(Mailbox(SECOND),))
    assert pick_sender(p, [MAIN, SECOND]).address == SECOND


def test_with_no_match_the_main_address_is_used():
    p = ParentMessage(to=(Mailbox("list@x.org"),))
    assert pick_sender(p, [MAIN, SECOND]).address == MAIN


def test_the_display_name_goes_only_on_the_address_it_belongs_to():
    assert pick_sender(PARENT, [MAIN, SECOND], name="Danny").name == ""
    assert pick_sender(None, [MAIN, SECOND], name="Danny").name == "Danny"


def test_delivered_to_is_read_off_a_real_message():
    raw = (b"From: a@x.org\r\nTo: list@x.org\r\nDelivered-To: me@x.org\r\n"
           b"X-Original-To: alias@x.org\r\nSubject: s\r\n\r\nb\r\n")
    p = ParentMessage.from_headers(email.message_from_bytes(raw, policy=email.policy.default))
    assert [m.address for m in p.delivered_to] == ["me@x.org", "alias@x.org"]


# ------------------------------------------------------------------ the world

@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    monkeypatch.setattr(accounts, "_READY", set())
    db.init_db()
    db.set_setting("user_address", MAIN)
    db.set_setting("user_name", "Danny Park")
    # The second address is known because the user has sent from it: a row in
    # a Sent folder is the evidence identity.py looks for.
    db.upsert_emails([dict(
        id="sent1", conversation_id=None, subject="x", from_name="", from_address=SECOND,
        to_recipients="[]", cc_recipients="[]", received_at="2026-09-01T00:00:00+00:00",
        is_read=1, is_answered=0, is_flagged=0, has_attachments=0, importance="normal",
        web_link="", folder="Sent", body_preview="", body_text="", body_html="",
        source_path="", synced_at=db.now_iso())])

    imap = FakeIMAP(username=SECOND, password=PASSWORD, persistent=True,
                    mailboxes=[("\\HasNoChildren", "INBOX"),
                               ("\\HasNoChildren \\Sent", "Sent"),
                               ("\\HasNoChildren \\Drafts", "Drafts")])
    imap.start(); imap.ready.wait(5)
    servers = []

    def smtp(**kw) -> FakeSMTP:
        s = FakeSMTP(**kw); s.start(); s.ready.wait(5); servers.append(s)
        accounts.remember_config(SECOND, ServerConfig(
            "127.0.0.1", imap.port, "plain", "127.0.0.1", s.port, "plain",
            SECOND, SECOND, "test"))
        return s

    accounts.remember_config(SECOND, ServerConfig(
        "127.0.0.1", imap.port, "plain", "127.0.0.1", 9, "plain", SECOND, SECOND, "test"))
    # The settle wait is seconds long by design; a test has no server to wait for.
    monkeypatch.setattr(auto_transport, "save_copy",
                        functools.partial(imap_folders.save_copy, delays=(0.0,)))

    from app.routers import compose as compose_router

    class Source:
        def parent_message(self, email_id):
            return PARENT

    monkeypatch.setattr(compose_router, "get_source", lambda: Source())
    with TestClient(app) as client:
        yield client, imap, smtp
    imap.stop()
    for s in servers:
        s.stop()


def reply(client, text="Registering now."):
    return client.post("/api/compose/draft", json={
        "action": "reply", "email_id": "e1", "text": text}).json()


def test_a_fresh_install_sends_automatically():
    from app.config import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["mail_transport"] == "auto"


def test_the_reply_is_drafted_from_the_delivered_to_address(world):
    client, _, _ = world
    assert SECOND in reply(client)["summary"]["from"]


def test_the_first_send_asks_for_the_password_and_sends_nothing(world):
    client, imap, smtp = world
    fake = smtp()
    token = reply(client)["token"]
    got = client.post(f"/api/compose/{token}/send")
    assert got.status_code == 409
    assert got.json()["detail"]["needs_password"] == SECOND
    assert fake.payload == b"" and imap.appended == []
    assert client.get(f"/api/compose/{token}").json()["sent_at"] is None, "still sendable"


def test_a_wrong_password_is_not_saved(world):
    client, _, _ = world
    got = client.post("/api/compose/credentials", json={"address": SECOND, "password": "nope"}).json()
    assert got["ok"] is False and got["reason"] == "rejected"
    assert accounts.password(SECOND) == ""


def test_credentials_are_only_taken_for_your_own_addresses(world):
    client, _, _ = world
    got = client.post("/api/compose/credentials",
                      json={"address": "stranger@elsewhere.org", "password": "x"})
    assert got.status_code == 400


def test_the_password_is_stored_encrypted(world):
    client, _, _ = world
    client.post("/api/compose/credentials", json={"address": SECOND, "password": PASSWORD})
    with db.connect() as conn:
        blob = conn.execute("SELECT secret FROM send_accounts WHERE address = ?",
                            (SECOND,)).fetchone()["secret"]
    assert PASSWORD.encode() not in bytes(blob)
    assert accounts.password(SECOND) == PASSWORD


def test_with_the_password_it_delivers_directly_and_files_a_sent_copy(world):
    client, imap, smtp = world
    fake = smtp()
    client.post("/api/compose/credentials", json={"address": SECOND, "password": PASSWORD})
    token = reply(client)["token"]
    got = client.post(f"/api/compose/{token}/send").json()
    fake.join(timeout=5)
    assert got["outcome"]["delivered"] is True
    assert fake.mail_from == SECOND and "careers@example.edu" in fake.rcpt_to
    assert imap.appended and imap.appended[-1]["mailbox"] == "Sent"


def test_a_tenant_that_disallows_sending_falls_back_to_drafts(world):
    """Microsoft 365's refusal when a tenant has switched password submission
    off. The password is right; the door is shut. Asking for the password
    again would be wrong -- the answer is Drafts."""
    client, imap, smtp = world
    smtp(auth_ok=False, auth_reply="535 5.7.139 Authentication unsuccessful, "
                                   "SmtpClientAuthentication is disabled for the Tenant.")
    client.post("/api/compose/credentials", json={"address": SECOND, "password": PASSWORD})
    got = client.post(f"/api/compose/{reply(client)['token']}/send").json()
    assert got["outcome"]["delivered"] is False
    assert got["outcome"]["handoff"] == "Drafts"
    drop = imap.appended[-1]
    assert drop["mailbox"] == "Drafts" and "\\Draft" in drop["flags"]
    msg = email.message_from_bytes(drop["message"], policy=email.policy.default)
    assert msg["In-Reply-To"] == "<talk@careers.example.edu>", "the draft must still thread"


def test_an_unreachable_sending_server_falls_back_to_drafts(world):
    client, imap, _ = world          # SMTP configured on port 9: nothing listens
    client.post("/api/compose/credentials", json={"address": SECOND, "password": PASSWORD})
    got = client.post(f"/api/compose/{reply(client)['token']}/send").json()
    assert got["outcome"]["handoff"] == "Drafts"


def test_a_password_both_servers_reject_asks_again(world):
    client, imap, smtp = world
    smtp(auth_ok=False)
    accounts.remember_password(SECOND, "stale-password")     # was right once; changed since
    got = client.post(f"/api/compose/{reply(client)['token']}/send")
    assert got.status_code == 409
    assert got.json()["detail"]["reason"] == "rejected"
    assert imap.appended == []


def test_a_provider_that_takes_no_password_is_told_plainly(world):
    client, _, _ = world
    accounts.remember_config(SECOND, ServerConfig(
        "outlook.office365.com", 993, "ssl", "smtp.office365.com", 587, "starttls",
        SECOND, SECOND, "mx:outlook.com", oauth_only=True))
    accounts.remember_password(SECOND, PASSWORD)
    got = client.post(f"/api/compose/{reply(client)['token']}/send")
    assert got.status_code == 502
    assert "password" in got.json()["detail"] and "Nothing was sent" in got.json()["detail"]


def test_a_forgotten_password_is_asked_for_again(world):
    client, _, _ = world
    client.post("/api/compose/credentials", json={"address": SECOND, "password": PASSWORD})
    got = client.delete(f"/api/compose/credentials/{SECOND}").json()
    assert accounts.password(SECOND) == ""
    assert [a["ready"] for a in got["accounts"] if a["address"] == SECOND] == [False]
    assert client.post(f"/api/compose/{reply(client)['token']}/send").status_code == 409


def test_settings_lists_what_automatic_sending_knows_without_secrets(world):
    client, _, _ = world
    client.post("/api/compose/credentials", json={"address": SECOND, "password": PASSWORD})
    status = client.get("/api/transports").json()["statuses"]["auto"]
    assert [a["address"] for a in status["accounts"]] == [SECOND]
    assert PASSWORD not in str(status)
