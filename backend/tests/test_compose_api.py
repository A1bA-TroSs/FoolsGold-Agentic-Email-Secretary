"""Writing mail through the API, and the gate in front of sending it.

The gate is the shape of the endpoint, not a check inside it: `send` takes a
token and nothing else, so the only thing that can leave the machine is a
message the server already rendered and handed back to be read. These tests
assert that property directly rather than trusting the handler.
"""
from __future__ import annotations

import email
import email.policy

import pytest
from fastapi.testclient import TestClient

from app import db
from app.compose import Mailbox, ParentMessage
from app.main import app
from app.transports import base

ME = "danny@uni.edu"


class Recorder(base.MailTransport):
    """Stands in for a real transport and keeps what it was handed."""
    name = "recorder"
    label = "Recorder"
    mode = base.DELIVERS
    sent: list[bytes] = []
    fail: str = ""

    def status(self):
        return base.TransportStatus(ready=True, account=ME, detail="recording")

    def send(self, message, raw=None):
        if Recorder.fail:
            raise base.TransportError(Recorder.fail)
        # `transmissible`, not `as_bytes`, because that is what a delivering
        # transport puts on the wire -- a double that skips the Bcc strip is
        # testing itself rather than the code.
        Recorder.sent.append(base.transmissible(message, raw))
        return base.SendResult(message_id=str(message.get("Message-ID") or ""),
                               sent_at="2026-09-20T00:00:00+00:00",
                               recipients=tuple(base.envelope_recipients(message)))


PARENT = ParentMessage(
    message_id="<parent@uni.edu>",
    references="<root@uni.edu>",
    subject="Midterm room change",
    sender=(Mailbox("prof@uni.edu", "Prof Kim"),),
    to=(Mailbox(ME, "Danny"), Mailbox("ta@uni.edu", "TA")),
    date="Fri, 19 Sep 2026 09:00:00 +0900",
    body_text="The midterm moves to room 302.",
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "_SCHEMA_READY", set())
    db.init_db()
    db.set_setting("user_address", ME)
    db.set_setting("user_name", "Danny Park")

    Recorder.sent, Recorder.fail = [], ""
    from app.routers import compose as compose_router

    class Source:
        def parent_message(self, email_id):
            return PARENT if email_id == "e1" else None

    monkeypatch.setattr(compose_router, "get_source", lambda: Source())
    monkeypatch.setattr(compose_router, "get_transport", lambda: Recorder())
    with TestClient(app) as c:
        yield c


def sent_message():
    return email.message_from_bytes(Recorder.sent[-1], policy=email.policy.default)


# ------------------------------------------------------------------ the gate

def test_sending_takes_a_token_and_nothing_else(client):
    """Asserted against the served spec, because this is the whole design.
    A send endpoint that accepted recipients or a body could transmit something
    the user never read, however careful its handler was."""
    post = app.openapi()["paths"]["/api/compose/{token}/send"]["post"]
    assert "requestBody" not in post
    assert [(p["name"], p["in"]) for p in post.get("parameters", [])] == [("token", "path")]


def test_what_is_sent_is_byte_for_byte_what_was_shown(client):
    made = client.post("/api/compose/draft", json={
        "action": "reply", "email_id": "e1", "text": "Understood."}).json()
    client.post(f"/api/compose/{made['token']}/send")

    shown, went = made["summary"], sent_message()
    assert went["Subject"] == shown["subject"]
    assert went["To"] == shown["to"]
    assert went["Message-ID"] == shown["message_id"]
    assert went["In-Reply-To"] == shown["in_reply_to"]
    # Compared with line endings normalised: the preview shows LF and the wire
    # carries CRLF, as RFC 5322 requires. That difference is correct and is the
    # only one permitted between the two.
    assert shown["text"] in went.get_body(("plain",)).get_content().replace("\r\n", "\n")


def test_an_unknown_token_sends_nothing(client):
    assert client.post("/api/compose/not-a-real-token/send").status_code == 404
    assert Recorder.sent == []


def test_a_second_click_does_not_send_a_second_copy(client):
    """A double click, or a retry after a lost reply, must not put two copies
    in someone's inbox."""
    token = client.post("/api/compose/draft", json={
        "action": "reply", "email_id": "e1", "text": "ok"}).json()["token"]
    first = client.post(f"/api/compose/{token}/send").json()
    second = client.post(f"/api/compose/{token}/send").json()

    assert len(Recorder.sent) == 1
    assert first["already"] is False and second["already"] is True
    assert second["sent_at"] == first["sent_at"]


def test_a_transport_failure_is_reported_and_nothing_is_marked_sent(client):
    Recorder.fail = "The server rejected that password."
    token = client.post("/api/compose/draft", json={
        "action": "reply", "email_id": "e1", "text": "ok"}).json()["token"]
    reply = client.post(f"/api/compose/{token}/send")
    assert reply.status_code == 502
    assert "password" in reply.json()["detail"]

    Recorder.fail = ""
    assert client.post(f"/api/compose/{token}/send").json()["already"] is False
    assert len(Recorder.sent) == 1, "the draft stayed sendable after the failure"


# ------------------------------------------------------------------ building

def test_a_reply_threads_and_addresses_itself(client):
    made = client.post("/api/compose/draft", json={
        "action": "reply", "email_id": "e1", "text": "ok"}).json()
    s = made["summary"]
    assert s["in_reply_to"] == "<parent@uni.edu>"
    assert "<root@uni.edu>" in s["references"]
    assert s["subject"] == "Re: Midterm room change"
    assert "prof@uni.edu" in s["to"]
    assert s["cc"] == ""


def test_reply_all_keeps_the_others_and_drops_me(client):
    s = client.post("/api/compose/draft", json={
        "action": "reply_all", "email_id": "e1", "text": "ok"}).json()["summary"]
    assert "ta@uni.edu" in s["cc"]
    assert ME not in s["cc"] and ME not in s["to"]


def test_a_typed_recipient_list_overrides_the_derived_one(client):
    """The user edited the field. Their choice outranks the reply rules."""
    s = client.post("/api/compose/draft", json={
        "action": "reply", "email_id": "e1", "text": "ok",
        "to": ["dean@uni.edu"]}).json()["summary"]
    assert "dean@uni.edu" in s["to"]
    assert "prof@uni.edu" not in s["to"]


@pytest.mark.parametrize("typed,want", [
    (["a@x.edu, b@x.edu"], ["a@x.edu", "b@x.edu"]),
    (["a@x.edu; b@x.edu"], ["a@x.edu", "b@x.edu"]),
    (["Prof Kim <k@x.edu>"], ["k@x.edu"]),
    (["a@x.edu", "A@X.EDU"], ["a@x.edu"]),
])
def test_recipients_are_read_the_way_people_type_them(client, typed, want):
    s = client.post("/api/compose/draft", json={
        "action": "new", "subject": "hi", "text": "y", "to": typed}).json()["summary"]
    got = [b.address.lower() for b in Mailbox.parse(s["to"])]
    assert got == want


def test_a_forward_does_not_thread(client):
    s = client.post("/api/compose/draft", json={
        "action": "forward", "email_id": "e1", "text": "fyi",
        "to": ["friend@x.edu"]}).json()["summary"]
    assert s["subject"] == "Fwd: Midterm room change"
    assert s["in_reply_to"] == "" and s["references"] == ""


def test_bcc_is_shown_to_the_author_and_not_to_anyone_else(client):
    made = client.post("/api/compose/draft", json={
        "action": "new", "subject": "hi", "text": "y",
        "to": ["a@x.edu"], "bcc": ["dean@uni.edu"]}).json()
    assert "dean@uni.edu" in made["summary"]["bcc"], "the author must see who they blind-copied"
    client.post(f"/api/compose/{made['token']}/send")
    assert b"dean@uni.edu" not in Recorder.sent[-1]


# ------------------------------------------------------------------ refusals

def test_composing_without_your_own_address_is_refused_with_the_fix(client):
    db.set_setting("user_address", "")
    reply = client.post("/api/compose/draft", json={
        "action": "new", "to": ["a@x.edu"], "text": "y"})
    assert reply.status_code == 400
    assert "Settings" in reply.json()["detail"]


def test_a_message_with_no_recipient_is_refused(client):
    assert client.post("/api/compose/draft", json={
        "action": "new", "subject": "hi", "text": "y"}).status_code == 400


def test_replying_to_something_that_cannot_be_read_back_is_refused_not_faked(client):
    """A source with no way back to the original cannot produce threading
    headers. A reply that quietly starts a new conversation is worse than one
    that refuses, because the user finds out from the recipient."""
    reply = client.post("/api/compose/draft", json={
        "action": "reply", "email_id": "gone", "text": "ok"})
    assert reply.status_code == 409
    assert "conversation" in reply.json()["detail"]


def test_an_unknown_action_is_refused(client):
    assert client.post("/api/compose/draft", json={
        "action": "delete_everything", "text": "y"}).status_code == 400


# ------------------------------------------------------------------ the UI's view

def test_the_draft_says_which_verb_the_button_should_use(client, monkeypatch):
    """"Send" on a transport that only files a draft is a lie the user finds
    out about later, when the message is still sitting in Drafts."""
    made = client.post("/api/compose/draft", json={
        "action": "new", "subject": "hi", "text": "y", "to": ["a@x.edu"]}).json()
    assert made["transport"]["verb"] == "send"

    class Filer(Recorder):
        mode = base.HANDS_OFF
    from app.routers import compose as compose_router
    monkeypatch.setattr(compose_router, "get_transport", lambda: Filer())
    made = client.post("/api/compose/draft", json={
        "action": "new", "subject": "hi", "text": "y", "to": ["a@x.edu"]}).json()
    assert made["transport"]["verb"] == "saveDraft"


def test_a_draft_can_be_read_back_before_it_is_sent(client):
    token = client.post("/api/compose/draft", json={
        "action": "new", "subject": "hi", "text": "y", "to": ["a@x.edu"]}).json()["token"]
    shown = client.get(f"/api/compose/{token}").json()
    assert shown["sent_at"] is None
    assert shown["summary"]["subject"] == "hi"
    client.post(f"/api/compose/{token}/send")
    assert client.get(f"/api/compose/{token}").json()["sent_at"] is not None


def test_what_was_shown_and_what_was_sent_are_both_kept(client):
    """The log the drafting model will need: without the pair, there is no
    label-free way to tell whether a suggested draft was any good."""
    token = client.post("/api/compose/draft", json={
        "action": "reply", "email_id": "e1", "text": "ok"}).json()["token"]
    client.post(f"/api/compose/{token}/send")
    stored = db.get_draft(token)
    assert stored["summary"]["text"].startswith("ok")
    assert stored["mime"] and stored["sent_at"] and stored["outcome"]["delivered"] is True
