"""F3: the classify prompt knows who the user is.

Address and role go to every provider; the people the user writes to most go
only to a local model (Danny, 2026-11-02: "Only for Ollama (local)")."""
from __future__ import annotations

import asyncio
import json

import pytest

from app import db, pipeline
from app.llm import base
from app.llm.base import UserContext, build_classify_prompt, reaches_user
from app.llm.ollama_provider import OllamaProvider

ME = "danny@snu.ac.kr"


def mail(i, **over):
    row = dict(id=f"m{i}", conversation_id=None, subject="Re: thesis chapter 3", from_name="Prof Kim",
               from_address="kim@snu.ac.kr", to_recipients=json.dumps([ME]), cc_recipients="[]",
               received_at="2026-11-01T09:00:00Z", is_read=0, is_answered=0, is_flagged=0,
               has_attachments=0, importance="normal", web_link="", folder="INBOX",
               body_preview="", body_text="Could you send the revised chapter?", body_html="", synced_at="")
    row.update(over)
    return row


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return tmp_path


class Recorder(base.LLMProvider):
    name = model = "rec"

    def __init__(self, local=False):
        self.local = local
        self.prompts: list[str] = []

    async def complete(self, system, user):
        self.prompts.append(user)
        ids = [line.split(": ", 1)[1] for line in user.splitlines() if line.startswith("id: ")]
        return json.dumps([{"id": i, "bucket": "fyi", "deadline": None, "tasks": [], "matched": [],
                            "category": "", "summary": "", "rationale": "x"} for i in ids])


class Source:
    def correspondents(self):
        return {"kim@snu.ac.kr": 9, ME.upper(): 50, "no-reply@snu.ac.kr": 30, "lee@x.com": 2,
                **{f"p{i}@x.com": 1 for i in range(15)}}


def head(prompt: str) -> str:
    return prompt.split("--- EMAIL")[0]


@pytest.mark.parametrize("over,want", [
    ({}, "to you"),
    ({"to_recipients": json.dumps(["lab@snu.ac.kr"]), "cc_recipients": json.dumps([ME])}, "cc you"),
    ({"to_recipients": json.dumps(["cs-grad@snu.ac.kr"])}, "not addressed to you directly (list or bcc)"),
    ({"to_recipients": json.dumps([{"address": ME.upper()}])}, "to you"),
])
def test_reaches_user(over, want):
    assert reaches_user(mail(1, **over), ME) == want


def test_no_address_means_no_reach_line():
    prompt = build_classify_prompt([mail(1)], [], UserContext())
    assert "reaches the user" not in prompt and "About the user" not in prompt


def test_prompt_without_context_is_unchanged():
    assert build_classify_prompt([mail(1)], ["thesis"]) == build_classify_prompt([mail(1)], ["thesis"], None)
    assert "About the user" not in build_classify_prompt([mail(1)], ["thesis"])


def test_context_block_contents_and_bounds():
    ctx = UserContext(address=ME, role="  PhD   student " + "x" * 300,
                      correspondents=[(f"Person {i}", f"p{i}@x.com") for i in range(20)])
    block = head(build_classify_prompt([mail(1)], [], ctx))
    assert ME in block and "role, in their words: PhD student" in block
    assert sum(f"<p{i}@x.com>" in block for i in range(20)) == 10
    assert len(block) < 1200


def test_empty_role_has_no_role_line():
    assert "role" not in head(build_classify_prompt([mail(1)], [], UserContext(address=ME))).lower()


def test_cloud_provider_never_gets_correspondents(store, monkeypatch):
    monkeypatch.setattr("app.sources.registry.get_source", lambda *a, **k: Source())
    db.set_setting("user_address", ME)
    db.set_setting("user_role", "PhD student in CS at SNU")
    ctx = pipeline.user_context(Recorder(local=False))
    assert ctx.address == ME and ctx.role == "PhD student in CS at SNU" and ctx.correspondents == []


def test_local_provider_gets_ranked_correspondents_without_self_or_system(store, monkeypatch):
    monkeypatch.setattr("app.sources.registry.get_source", lambda *a, **k: Source())
    db.set_setting("user_address", ME)
    db.upsert_emails([mail(1)])
    ctx = pipeline.user_context(Recorder(local=True))
    addresses = [a for _, a in ctx.correspondents]
    assert addresses[:2] == ["kim@snu.ac.kr", "lee@x.com"]
    assert ME not in addresses and "no-reply@snu.ac.kr" not in addresses
    assert len(addresses) == 10
    assert ctx.correspondents[0] == ("Prof Kim", "kim@snu.ac.kr")


def test_setting_off_drops_correspondents_for_local_too(store, monkeypatch):
    monkeypatch.setattr("app.sources.registry.get_source", lambda *a, **k: Source())
    db.set_setting("share_correspondents", "0")
    assert pipeline.user_context(Recorder(local=True)).correspondents == []


def test_source_failure_does_not_break_context(store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no mail store")
    monkeypatch.setattr("app.sources.registry.get_source", boom)
    db.set_setting("user_address", ME)
    assert pipeline.user_context(Recorder(local=True)) == UserContext(address=ME)


def test_ollama_is_the_only_local_provider():
    from app.llm.api_providers import AnthropicProvider, OpenAIProvider
    from app.llm.copilot_provider import CopilotProvider
    assert OllamaProvider.local is True
    assert not any(cls.local for cls in (AnthropicProvider, OpenAIProvider, CopilotProvider))


@pytest.mark.parametrize("local", [False, True])
def test_classify_pending_and_upgrade_send_context(store, monkeypatch, local):
    monkeypatch.setattr("app.sources.registry.get_source", lambda *a, **k: Source())
    db.set_setting("user_address", ME)
    db.set_setting("user_role", "PhD student")
    rec = Recorder(local=local)
    monkeypatch.setattr(pipeline, "get_provider", lambda: rec)
    db.upsert_emails([mail(1), mail(2, to_recipients=json.dumps(["list@snu.ac.kr"]))])
    asyncio.run(pipeline.classify_pending(limit=10))
    first = rec.prompts[0]
    assert "role, in their words: PhD student" in head(first)
    assert ("kim@snu.ac.kr" in head(first)) is local
    assert "reaches the user: to you" in first
    assert "reaches the user: not addressed to you directly" in first

    with db.connect() as conn:
        conn.execute("DELETE FROM classifications")
    emails = db.get_emails(["m1", "m2"])
    pipeline._persist(pipeline._structural_classifications(emails, ME),
                      {e["id"]: e for e in emails}, "structural", "none")
    rec.prompts.clear()
    asyncio.run(pipeline._upgrade_structural(rec, ME, [], 10, budget=10))
    assert rec.prompts and "PhD student" in head(rec.prompts[0])
    assert ("kim@snu.ac.kr" in head(rec.prompts[0])) is local


def test_system_prompt_rules_lean_but_never_override():
    rules = base.CLASSIFY_SYSTEM
    assert "only lean the bucket" in rules and "a stated deadline or a task for the" in rules
