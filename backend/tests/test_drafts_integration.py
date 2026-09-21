"""Compose to Drafts over a real IMAP socket, through every layer this app owns.

Nothing is mocked between the HTTP request and the bytes on the wire: the
compose route builds the message, the registry picks the transport from saved
settings, `open_imap` connects, discovery finds Drafts by its RFC 6154
attribute, and `APPEND` carries the message across as a literal. The only
thing standing in for the real world is the server.
"""
from __future__ import annotations

import email
import email.policy
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))
from fake_imap import FakeIMAP                                   # noqa: E402

from app import db                                               # noqa: E402
from app.compose import Mailbox, ParentMessage                   # noqa: E402
from app.main import app                                         # noqa: E402

USER, PASSWORD = "danny@example.edu", "app-password"

PARENT = ParentMessage(
    message_id="<announcement-17@careers.example.edu>",
    references="<announcement-08@careers.example.edu>",
    subject="“Ethics in Practice” ICAC Seminar",
    sender=(Mailbox("careers@example.edu", "Career Development Programs"),),
    to=(Mailbox(USER, "Danny"),),
    date="Thu, 17 Sep 2026 09:00:00 +0800",
    body_text="The seminar is on 20 September. Please register in advance.",
)


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    db.init_db()
    server = FakeIMAP(username=USER, password=PASSWORD, persistent=True)
    server.start()
    server.ready.wait(timeout=5)

    from app.routers import compose as compose_router

    class Source:
        def parent_message(self, email_id):
            return PARENT if email_id == "e1" else None

    monkeypatch.setattr(compose_router, "get_source", lambda: Source())

    def configure(**over):
        values = {"user_address": USER, "user_name": "Danny Park",
                  "mail_transport": "imap_draft", "imap_host": "127.0.0.1",
                  "imap_port": str(server.port), "imap_security": "plain",
                  "imap_username": USER}
        values.update(over)
        with TestClient(app) as c:
            c.patch("/api/settings", json={"values": {**values, "smtp_password": PASSWORD}})
    configure()
    with TestClient(app) as client:
        yield client, server, configure
    server.stop()


def stored(server) -> email.message.Message:
    return email.message_from_bytes(server.appended[-1]["message"], policy=email.policy.default)


# ------------------------------------------------------------------ the path

def test_a_reply_written_here_arrives_in_drafts_threaded(world):
    client, server, _ = world
    made = client.post("/api/compose/draft", json={
        "action": "reply", "email_id": "e1", "text": "Registering now, thank you."}).json()
    assert made["transport"]["verb"] == "saveDraft"

    result = client.post(f"/api/compose/{made['token']}/send").json()

    assert result["outcome"]["delivered"] is False
    assert result["outcome"]["handoff"] == "Drafts"
    assert len(server.appended) == 1
    drop = server.appended[0]
    assert drop["mailbox"] == "Drafts"
    assert "\\Draft" in drop["flags"], "without it the client will not open it for editing"

    msg = stored(server)
    assert msg["In-Reply-To"] == "<announcement-17@careers.example.edu>"
    assert "<announcement-08@careers.example.edu>" in msg["References"]
    assert msg["Subject"] == "Re: “Ethics in Practice” ICAC Seminar"
    assert "careers@example.edu" in msg["To"]
    assert "Registering now" in msg.get_body(("plain",)).get_content()


def test_what_arrives_is_byte_for_byte_what_was_approved(world):
    """The approval gate, followed all the way to the server -- with no
    normalisation. The first version of this test compared after mapping CRLF
    to LF and still failed: the send path re-parsed and re-serialised the
    approved bytes, which re-folded `References:` with a trailing space.
    Semantically identical, and not what the user approved."""
    client, server, _ = world
    made = client.post("/api/compose/draft", json={
        "action": "reply", "email_id": "e1", "text": "ok"}).json()
    client.post(f"/api/compose/{made['token']}/send")
    assert server.appended[-1]["message"] == bytes(db.get_draft(made["token"])["mime"])


def test_bcc_survives_into_the_draft(world):
    """On a handoff there is no envelope yet -- the user's client builds one
    when they press send -- so stripping Bcc here would drop those people."""
    client, server, _ = world
    made = client.post("/api/compose/draft", json={
        "action": "new", "subject": "hi", "text": "y",
        "to": ["a@example.edu"], "bcc": ["dean@example.edu"]}).json()
    client.post(f"/api/compose/{made['token']}/send")
    assert "dean@example.edu" in stored(server)["Bcc"]


def test_a_second_click_files_one_draft_not_two(world):
    client, server, _ = world
    token = client.post("/api/compose/draft", json={
        "action": "reply", "email_id": "e1", "text": "ok"}).json()["token"]
    client.post(f"/api/compose/{token}/send")
    client.post(f"/api/compose/{token}/send")
    assert len(server.appended) == 1


# ------------------------------------------------------------------ the test button

def test_the_settings_test_signs_in_finds_drafts_and_writes_nothing(world):
    client, server, _ = world
    got = client.post("/api/transports/test").json()
    assert got["ok"] is True
    assert got["folder"] == "Drafts"
    assert server.appended == [], "a connection test must not leave a draft behind"
    assert server.logins == [(USER, PASSWORD)]


def test_a_wrong_password_is_reported_in_words_the_user_can_act_on(world):
    client, server, configure = world
    configure()
    with TestClient(app) as c:
        c.patch("/api/settings", json={"values": {"smtp_password": "wrong"}})
    got = client.post("/api/transports/test").json()
    assert got["ok"] is False
    assert "app-specific password" in got["detail"]


def test_a_server_that_will_not_starttls_never_sees_the_password(world):
    """The password would otherwise cross the network in the clear."""
    client, server, configure = world
    configure(imap_security="starttls")
    got = client.post("/api/transports/test").json()
    assert got["ok"] is False
    assert "STARTTLS" in got["detail"]
    assert server.logins == []


def test_an_unreachable_server_is_named(world):
    client, _, configure = world
    configure(imap_port="9")
    got = client.post("/api/transports/test").json()
    assert got["ok"] is False and "127.0.0.1" in got["detail"]


def test_a_mailbox_with_no_drafts_folder_says_where_to_fix_it(world):
    client, server, _ = world
    server.mailboxes = [("\\HasNoChildren", "INBOX")]
    got = client.post("/api/transports/test").json()
    assert got["ok"] is False and "Settings" in got["detail"]


def test_an_override_is_used_when_discovery_would_guess_wrong(world):
    client, server, configure = world
    server.mailboxes.append(("\\HasNoChildren", "Mine/Replies"))
    configure(drafts_folder="Mine/Replies")
    assert client.post("/api/transports/test").json()["folder"] == "Mine/Replies"


# ------------------------------------------------------------------ the mask

def test_saving_the_settings_form_does_not_save_the_mask_as_the_password(world):
    """`all_settings()` hands a saved secret back as `********`. A form that
    round-trips its fields would save that as the password, and the next login
    would fail with the user certain they had typed it correctly."""
    client, server, _ = world
    shown = client.get("/api/settings").json()
    assert shown["smtp_password"] == "********"
    client.patch("/api/settings", json={"values": {"smtp_password": shown["smtp_password"],
                                                   "imap_host": "127.0.0.1"}})
    assert db.get_setting("smtp_password") == PASSWORD
    assert client.post("/api/transports/test").json()["ok"] is True


def test_a_check_that_crashes_is_reported_not_turned_into_a_500(world, monkeypatch):
    """A test button that answers with a server error tells the user the app
    is broken, not that their settings are. Anything unexpected is still an
    answer to show."""
    client, _, _ = world
    from app.routers import config as config_router

    class Exploding:
        name, mode = "imap_draft", "hands_off"
        def check(self):
            raise RuntimeError("socket library had a bad day")

    monkeypatch.setattr(config_router, "get_transport", lambda name=None: Exploding())
    reply = client.post("/api/transports/test")
    assert reply.status_code == 200
    assert reply.json()["ok"] is False and "bad day" in reply.json()["detail"]
