#!/usr/bin/env python3
"""A fake backend, so the UI can be rendered in states that are hard to reach.

The frontend was verified for six rounds by parsing it, and parsing found none
of the defects: a badge that could never appear, a translation silently
overwritten by a spread, a badge wrapping to two lines. None of those are syntax
errors. All three are obvious in a screenshot.

This serves `dist/` plus fixture JSON on the same origin, so the real bundle
runs against real HTTP with no Electron and no mailbox. Fixtures are chosen to
force the states that matter -- an explored row, a stale deadline, a reason of
each kind -- rather than whatever a live inbox happens to contain.
"""
from __future__ import annotations

import json
import re
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
LANG = sys.argv[2] if len(sys.argv) > 2 else "ko"
THEME = sys.argv[3] if len(sys.argv) > 3 else "gold"

def mail(i, **over):
    base = {
        "id": f"m{i}", "subject": "Subject", "from_name": "Sender",
        "from_address": "a@b.com", "received_at": "2026-09-16T09:00:00+00:00",
        "is_read": 0, "is_answered": 0, "is_flagged": 0, "has_attachments": 0,
        "importance": "normal", "body_preview": "Preview text.",
        "bucket": "fyi", "deadline": None, "score": 50, "matched": [],
        "rationale": "", "source": "llm", "explored": 0, "copies": 1,
        "reason_code": "none", "reason_arg": "", "verdict": None,
        "snooze_until": None, "highlight": None, "decided_at": None, "category": "",
    }
    base.update(over)
    return base

MAIL = [
    mail(1, subject="[Invitation] Recruitment Talk – Huawei Technologies Ltd",
         from_name="Center for Industry Engagement", bucket="action", score=140,
         deadline="2026-09-18", reason_code="topic", reason_arg="CO-OP", matched=["CO-OP"]),
    mail(2, subject="17일 워크숍, Career Center 등록 여부 바로 확인하세요.",
         from_name="Career Development Programs", bucket="action", score=130,
         deadline="2026-09-17", reason_code="deadline", reason_arg="1"),
    mail(3, subject="Final Reminder: submit the progress report",
         bucket="action", score=120, reason_code="direct",
         # A collapsed announcement. The backend hides the other copies from
         # the ranked list, so the harness has to render the thing that says
         # so -- a list that quietly drops mail looks identical to one that
         # lost it, and only this count tells them apart.
         copies=3),
    mail(4, subject="A very long subject line that should truncate rather than "
                    "push the row wider than the pane it lives in, forever",
         bucket="fyi", score=60, reason_code="affinity"),
    mail(5, subject="Weekly round-up: 40% off boots", from_name="ShopCo",
         from_address="noreply@shop.com", bucket="noise", score=8,
         explored=1, reason_code="explored"),
    mail(6, subject="스터디 모임 공지 - 이번 주 목요일", bucket="fyi", score=55,
         reason_code="flagged", is_flagged=1),
    mail(7, subject="Pinned by you", bucket="fyi", score=200,
         reason_code="pinned", verdict="pinned"),
]

# A realistic mailbox. Seven rows hid a stall that only shows at scale: the
# FLIP reorder measures every row twice per change, so its cost is linear in
# list length and invisible on a fixture.
import os as _os
_BULK = int(_os.environ.get("FG_BULK", "0"))
if _BULK:
    MAIL = MAIL + [
        mail(100 + i, subject=f"[{i}] Recruitment Talk, workshop and reminder {i}",
             from_name=f"Sender {i % 17}", from_address=f"s{i % 17}@uni.edu",
             bucket=("action" if i % 3 == 0 else "fyi"), score=100 - (i % 90),
             reason_code=("topic" if i % 4 == 0 else "direct"),
             reason_arg=("CO-OP" if i % 4 == 0 else ""))
        for i in range(_BULK)
    ]

FIXTURES = {
    "/api/health": {"ok": True, "app": "Fools Gold", "version": "1.0.0",
                    "source": {"ready": True, "kind": "applemail", "detail": ""},
                    # The state the report came from: a local model configured,
                    # and a model name this Mac does not have. Keyed, because
                    # the banner used to bolt an English sentence into the
                    # middle of a Korean one.
                    "ai": {"available": False, "off": False,
                           "detail": "Ollama has no model called 'qwen3.5:9b'.",
                           "detail_key": "aiModelMissingHave",
                           "detail_vars": {"model": "qwen3.5:9b", "have": "qwen3.5:4b"}}},
    # Shaped like the real endpoint (`current` + `statuses`), not like a
    # plausible guess. A fixture that disagrees with the API tests a different
    # app -- this one said `active`/`sources` and the settings screen, which
    # reads `statuses`, had never once been rendered by the harness.
    "/api/sources": {"current": "applemail", "statuses": {
        "applemail": {"ready": True, "needs_auth": False, "detail": "~/Library/Mail"},
        "graph": {"ready": False, "needs_auth": True, "detail": "not signed in"}}},
    "/api/auth/status": {"signed_in": True, "email": "me@x.com"},
    "/api/mail": {"items": MAIL, "counts": {"action": 3, "fyi": 3, "noise": 1},
                  "last_sync": "2026-09-16T09:00:00+00:00",
                  # `aiStatus` is read from HERE, not from /api/health -- see
                  # App.jsx `setAiStatus(data.ai || ...)` on the list response.
                  # The fixture carried an `ai` block on /api/health only, so
                  # the AI banner had never rendered in this harness. Sixth
                  # fixture-fidelity bug, same shape as the other five: green
                  # about a thing it was not showing.
                  "ai": {"available": False, "off": False,
                         "detail": "Ollama has no model called 'qwen3.5:9b'.",
                         "detail_key": "aiModelMissingHave",
                         "detail_vars": {"model": "qwen3.5:9b", "have": "qwen3.5:4b"}},
                  # The frozen case, because it is the one that used to render
                  # as an ordinary quiet inbox and is therefore the one worth
                  # having a screenshot of in every theme and language.
                  "sync": {"state": "frozen", "at": "2026-09-19T04:14:02+00:00",
                           "trigger": "poller", "error": "", "consecutive_failures": 0,
                           "newest_received": "2026-09-14T09:32:43+00:00",
                           "frozen_since": "2026-09-14T10:00:00+00:00",
                           "frozen_checks": 61, "frozen_hours": 116.0}},
    "/api/ranking/categories": {"taxonomy": ["exam", "coursework", "announcement", "career",
                                            "event", "competition", "admin", "service"],
        "categories": [
            {"category": "competition", "total": 8, "dismissed": 6, "done": 0, "pinned": 0,
             "weight": -1.42, "signals": 6},
            {"category": "exam", "total": 5, "dismissed": 0, "done": 3, "pinned": 2,
             "weight": 0.91, "signals": 5},
            {"category": "event", "total": 11, "dismissed": 4, "done": 0, "pinned": 0,
             "weight": -0.38, "signals": 4}]},
    "/api/mail/senders/muted": {"items": []},
    "/api/mail/senders/highlighted": {"items": []},
    "/api/mail/digest/today": {
        # Ordered the way the app now orders it: due today first, then later in
        # the window, and a recently-missed item only after those. A fixture
        # that contradicts the behaviour is a screenshot of a program nobody
        # runs -- which is exactly what the /api/sources fixture turned out to
        # be a moment ago.
        #
        # Fourth fixture-fidelity bug, and the one that hid a reported defect:
        # every row here was keyed `id`, and the real endpoint emits
        # `email_id` (pipeline._as_agenda_row). Digest.jsx reads `email_id`,
        # so every row rendered with an undefined React key and an Open that
        # opened nothing -- and the briefing that shipped in every screenshot
        # this harness ever took was one no backend produces.
        #
        # The rows below are the STRUCTURAL shape: `note_key` + `note_vars`,
        # which is what the app emits with no model, and what it falls back to
        # whenever a local Ollama run fails. That is the state the duplicated
        # title was reported in, so it is the state the harness has to render.
        "day": "2026-09-16", "headline": {"key": "agendaHeadline", "vars": {"n": 3, "d": 2}},
        "items": [
            {"email_id": "m2", "note": "", "note_key": "dueToday", "note_vars": {},
             "subject": "Professional Grooming for All", "sender": "Career Development Programs",
             "bucket": "action", "deadline": "2026-09-16", "done": False},
            {"email_id": "m1", "note": "", "note_key": "dueOn",
             "note_vars": {"date": "2026-09-18"},
             "subject": "[Tonight - Info Session] Recruitment of the HKUST Robotics Team",
             "sender": "Center for Global & Community Engagement",
             "bucket": "action", "deadline": "2026-09-18", "done": False},
            {"email_id": "m3", "note": "", "note_key": "needsReply", "note_vars": {},
             "subject": "Final Reminder: submit the progress report", "copies": 3,
             "bucket": "action", "sender": "Course Office", "deadline": None, "done": False},
        ],
        "model": "ollama:qwen3.5:9b", "created_at": "2026-09-16T08:00:00+00:00"},
    "/api/ollama/models": {"configured": "qwen3.5:9b",
                           "installed": ["qwen3.5:4b", "gemma3:4b"],
                           "matches": False},
    "/api/priorities": {"items": [
        {"id": 1, "topic": "CO-OP", "status": "active", "weight": 20},
        {"id": 2, "topic": "논문 심사", "status": "active", "weight": 15}]},
    "/api/priorities/suggestions": {"items": []},
    "/api/ranking": {
        "thresholds": {"relevance": 0.35, "action": 0.55, "moved_today": 0.0},
        "target_action_volume": "8", "explore_one_in": 20, "explored_count": 3,
        "learning": {"epoch_start": "", "enabled": False, "applied": 0,
                     "refused": 12, "refused_by_reason": {"pre-epoch": 9, "burst": 3},
                     "weights": {}}},
    "/api/calendar/month": {"weeks": [], "month": 9, "year": 2026},
    "/api/calendar/agenda": {"items": []},
    "/api/calendar/removed": {"items": []},
    "/api/settings/copilot-status": {"signed_in": True, "detail": "auto"},
}

# Cloud-AI consent. Off by default (the provider is local), so every other
# check runs exactly as before; `/__set?consent=pending` switches the provider
# to a cloud one that has not been allowed yet.
# The briefing is served the way the backend serves it: settled rows dropped
# and the gap refilled from the queue behind them (pipeline._with_current_verdicts).
# A static fixture here would put a ticked row straight back on the next read,
# and the harness would be testing a briefing no backend produces.
# The recap card: what piled up since the user last looked.
#
# Shaped like the real endpoint, including the parts that decide whether the
# card renders at all. `too_soon` is the app declining to speak, and the only
# thing that sets it here is the dismiss POST -- which is the whole state rule
# this fixture exists to exercise.
RECAP_SEEN: list = []


def recap_card() -> dict:
    lines = [
        {"email_id": "m1", "email_ids": ["m1"],
         "subject": "[Invitation] Recruitment Talk \u2013 Huawei Technologies Ltd",
         "task": "Register for the Huawei recruitment talk", "summary": "",
         "sender": "Center for Industry Engagement", "deadline": "2026-09-18",
         "received_at": "2026-09-16T09:00:00+00:00", "bucket": "action", "copies": 1},
        {"email_id": "m3", "email_ids": ["m3", "m6", "m7"],
         "subject": "Final Reminder: submit the progress report",
         "task": "Submit the Co-op progress report", "summary": "",
         "sender": "Co-op Office", "deadline": "2026-09-19",
         "received_at": "2026-09-16T08:00:00+00:00", "bucket": "action", "copies": 3},
    ]
    events = [
        {"email_id": "m4", "email_ids": ["m4"],
         "subject": "Seminar: Ethics in Practice", "task": "",
         "summary": "The ICAC seminar moves to Wednesday and registration closes Monday.",
         "sender": "Student Affairs", "deadline": None,
         "received_at": "2026-09-15T10:00:00+00:00", "bucket": "fyi", "copies": 1},
    ]
    return {
        "since": "2026-09-15T09:00:00+00:00",
        "clamped": False, "first_run": False,
        "too_soon": bool(RECAP_SEEN),
        "total": 4, "lines": 3,
        "sections": [
            {"key": "needs", "count": 2, "lines": lines, "truncated": False},
            {"key": "cat:event", "count": 1, "lines": events, "truncated": False},
        ],
        "bulk": {"key": "bulk", "count": 1, "more": 0, "senders": [
            {"sender": "ShopCo", "count": 1, "lines": [
                {"email_id": "m5", "email_ids": ["m5"],
                 "subject": "Weekly round-up: 40% off boots", "task": "",
                 "summary": "", "sender": "ShopCo", "deadline": None,
                 "received_at": "2026-09-14T10:00:00+00:00", "bucket": "noise",
                 "copies": 1}]}]},
        "hidden": {"hidden": 12, "explored": 1},
    }


DIGEST_SETTLED: set = set()
DIGEST_RESERVE = [
    {"email_id": "m4", "note": "", "note_key": "dueOn", "note_vars": {"date": "2026-09-19"},
     "subject": "Scholarship Application - UG", "sender": "Student Affairs",
     "bucket": "action", "deadline": "2026-09-19", "done": False},
    {"email_id": "m5", "note": "", "note_key": "dueOn", "note_vars": {"date": "2026-09-20"},
     "subject": "Talk. Lead. Get coached", "sender": "Center for Language Education",
     "bucket": "action", "deadline": "2026-09-20", "done": False},
]
DIGEST_MODEL = {"model": None}


def digest_now():
    base = dict(FIXTURES["/api/mail/digest/today"])
    rows = [r for r in base["items"] + DIGEST_RESERVE if r["email_id"] not in DIGEST_SETTLED]
    base["items"] = rows[:3]
    if DIGEST_MODEL["model"]:
        base["model"] = DIGEST_MODEL["model"]
    return base


CONSENT = {"state": "off"}
CONSENT_POSTS: list = []


def consent_status():
    if CONSENT["state"] == "off":
        return {"provider": SETTINGS.get("llm_provider", "ollama"), "required": False, "granted": True}
    return {"provider": "anthropic", "version": 1, "recipient": "Anthropic, PBC", "country": "US",
            "policy_url": "https://www.anthropic.com/legal/privacy", "required": True,
            "granted": CONSENT["state"] == "granted",
            "granted_at": "2026-09-21T10:00:00+00:00" if CONSENT["state"] == "granted" else None}


SETTINGS = {
    "theme": THEME, "ui_language": LANG, "mail_source": "applemail",
    "user_address": "me@x.com", "llm_provider": "ollama", "copilot_model": "auto",
    "ollama_model": "qwen3.5:9b", "ollama_host": "",
    "sync_days": "30", "sync_max_messages": "300", "classify_batch_size": "10",
    "theta_relevance": "0.35", "theta_action": "0.55", "target_action_volume": "8",
    "learning_epoch_start": "", "explore_one_in": "20", "auto_digest": "true",
    "notify_enabled": "true", "notify_morning": "09:00", "notify_evening": "21:00",
    "applemail_root": "", "applemail_inbox_only": "true", "entra_client_id": "",
    "anthropic_model": "claude-sonnet-4-5", "openai_model": "gpt-4o-mini",
    "openai_base_url": "",
}


# The transport the compose pane thinks it is talking to. Switchable, because
# the two modes render different words on the same button and the whole point
# of the handoff design is that the difference is visible before the click.
TRANSPORT = {"name": "smtp", "label": "SMTP", "mode": "delivers", "ready": True,
             "detail": "smtp.example.edu:587 (starttls)", "verb": "send"}

HANDOFF = {"name": "imap_draft", "label": "Save to Drafts", "mode": "hands_off",
           "ready": True, "detail": "imap.example.edu:993", "verb": "saveDraft"}


def compose_summary(body: dict) -> dict:
    """What the real backend returns: the message it just built, not a replay
    of the form. The review screen exists to show the headers the app filled
    in, so a stub that echoed the form back would prove nothing."""
    action = body.get("action", "new")
    reply = action in ("reply", "reply_all")
    typed = ", ".join(body.get("to") or []) or (
        "Career Development Programs <careers@example.edu>" if reply else "")
    return {
        "action": action,
        "from": "Danny Park <danny@example.edu>",
        "to": typed,
        "cc": ", ".join(body.get("cc") or []) or (
            "TA <ta@example.edu>" if action == "reply_all" else ""),
        "bcc": ", ".join(body.get("bcc") or []),
        "subject": ("Re: " if reply else "Fwd: " if action == "forward" else "")
                   + (body.get("subject") or "\u201cEthics in Practice\u201d ICAC Seminar"),
        "message_id": "<178987374224.1267.162619993@example.edu>",
        "in_reply_to": "<original-announcement@example.edu>" if reply else "",
        "references": "<thread-root@example.edu>" if reply else "",
        "text": (body.get("text") or "")
                + ("\n\nOn Thu, 17 Sep 2026 09:00:00 +0800, Career Development "
                   "Programs wrote:\n> The seminar is on 20 September. Please "
                   "register in advance.\n" if reply else ""),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(DIST), **kw)

    def log_message(self, *a):  # quiet
        pass

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/settings":
            return dict(SETTINGS)
        # Longest key first. Iterating the dict in declaration order meant
        # `/api/mail` matched `/api/mail/digest/today` as a prefix and answered
        # it with the mail list -- so the briefing pane in every screenshot
        # this harness has ever taken was rendering seven mail rows, and the
        # digest fixture below it had never once been served.
        #
        # Third fixture-fidelity bug in a day, after /api/sources returning the
        # wrong shape and the digest fixture contradicting the ordering. The
        # pattern is the same each time: the harness was green about a screen
        # it was not actually showing.
        for key in sorted(FIXTURES, key=len, reverse=True):
            if path == key or path.startswith(key + "/"):
                return FIXTURES[key]
        return None

    def _mail_query(self):
        """The completed box asks the same endpoint a different question, so the
        stub has to answer it differently -- otherwise the render check passes
        on the ordinary list and proves nothing about the box."""
        from urllib.parse import parse_qs
        q = parse_qs(urlparse(self.path).query)
        want = (q.get("verdict") or [None])[0]
        if want == "not_relevant":
            return {"items": [dict(m, verdict="not_relevant",
                                   category=["competition", "event", "service"][i % 3],
                                   decided_at="2026-09-18T12:00:00+00:00")
                              for i, m in enumerate(MAIL)],
                    "counts": FIXTURES["/api/mail"]["counts"],
                    "last_sync": "2026-09-16T09:00:00+00:00"}
        if want != "done":
            return None
        items = [dict(m, verdict="done",
                      decided_at="2026-09-16T18:30:00+00:00" if i % 2 else "2026-09-15T11:05:00+00:00")
                 for i, m in enumerate(MAIL)]
        return {"items": items, "counts": FIXTURES["/api/mail"]["counts"],
                "last_sync": "2026-09-16T09:00:00+00:00"}

    def do_GET(self):
        # Control endpoint for the harness. Theme and language reach the app
        # through /api/settings, which the app requests with no query string of
        # its own -- so putting them on the PAGE url did nothing, and sixteen
        # "theme x language" screenshots came out in one theme. The harness sets
        # them here first, then loads the page.
        if self.path.startswith("/__set"):
            from urllib.parse import parse_qs
            q = parse_qs(urlparse(self.path).query)
            if q.get("theme"):
                SETTINGS["theme"] = q["theme"][0]
            if q.get("lang"):
                SETTINGS["ui_language"] = q["lang"][0]
            if q.get("theme"):
                DIGEST_SETTLED.clear()      # every combination starts from the full briefing
            if q.get("recap"):
                RECAP_SEEN.clear()
            if q.get("theme"):
                RECAP_SEEN.clear()      # every combination starts with the card open
            if q.get("digest"):
                DIGEST_SETTLED.clear()
                DIGEST_MODEL["model"] = None if q["digest"][0] == "reset" else q["digest"][0]
            if q.get("setup"):
                # First-run screen: Apple Mail chosen, macOS still denying
                # ~/Library/Mail. Exactly the state a user who just downloaded
                # the DMG sees -- the one that shipped in English, unscrollable,
                # and addressed to a developer.
                setup_state = q["setup"][0]
                FIXTURES["/api/health"]["source"] = (
                    {"ready": True, "kind": "applemail", "detail": ""} if setup_state == "off" else
                    {"ready": False, "name": "applemail", "label": "Apple Mail", "needs_setup": True,
                     "detail": "macOS has not given FoolsGold access to your mail yet.",
                     "detail_key": "fdaNeeded", "detail_vars": {"launcher": "app"}})
            if q.get("consent"):
                CONSENT["state"] = q["consent"][0]
                CONSENT_POSTS.clear()
                SETTINGS["llm_provider"] = "ollama" if CONSENT["state"] == "off" else "anthropic"
            if q.get("transport"):
                TRANSPORT.clear()
                TRANSPORT.update(HANDOFF if q["transport"][0] == "hands_off"
                                 else {"name": "smtp", "label": "SMTP", "mode": "delivers",
                                       "ready": True, "detail": "smtp.example.edu:587 (starttls)",
                                       "verb": "send"})
            return self._json({"theme": SETTINGS["theme"], "lang": SETTINGS["ui_language"],
                               "transport": TRANSPORT["mode"]})
        if urlparse(self.path).path == "/__consent_posts":
            return self._json({"posts": CONSENT_POSTS})
        if urlparse(self.path).path == "/api/ai/consent":
            return self._json(consent_status())
        if urlparse(self.path).path == "/api/mail/digest/today":
            return self._json(digest_now())
        # One email, opened.
        #
        # `/api/mail/<id>` fell through the prefix rule and was answered with
        # the LIST, so the reading pane -- the reason line, the body frame, and
        # now the divider between them -- had never been rendered by this
        # harness at all. Fifth fixture-fidelity bug, and the same shape as the
        # other four: green about a screen it was not showing.
        detail = re.match(r"^/api/mail/(m\d+)$", urlparse(self.path).path)
        if detail:
            found = next((m for m in MAIL if m["id"] == detail.group(1)), None)
            if found is not None:
                return self._json(dict(
                    found,
                    to_recipients=[{"name": "Danny", "address": "me@x.com"}],
                    cc_recipients=[],
                    # Codes, exactly as the backend now sends them for a
                    # structurally ranked message -- so a raw key or an
                    # untranslated sentence on this line fails a check instead
                    # of reaching a screenshot.
                    rationale=json.dumps([
                        {"key": "sigDeadline", "vars": {"date": "2026-09-18"}},
                        {"key": "sigDirect"},
                        {"key": "sigHighImportance"},
                        {"key": "sigAttachment"},
                    ]),
                    source="structural",
                    body_html=("<p>Please disregard this email if you have already "
                               "registered.</p>" + "<p>Body line.</p>" * 40),
                ))

        if urlparse(self.path).path == "/api/mail/recap":
            return self._json(recap_card())
        if self.path.startswith("/api/mail?") or self.path == "/api/mail":
            special = self._mail_query()
            if special is not None:
                return self._json(special)
        if self.path.startswith("/api/"):
            payload = self._route()
            return self._json(payload if payload is not None else {}, 200 if payload else 404)
        return super().do_GET()

    def do_DELETE(self):
        fb = re.match(r"^/api/mail/([^/]+)/feedback$", urlparse(self.path).path)
        if fb:
            DIGEST_SETTLED.discard(fb.group(1))
            return self._json({"ok": True})
        if urlparse(self.path).path == "/api/ai/consent":
            CONSENT["state"] = "pending"
            return self._json({"withdrawn": 1, **consent_status()})
        return self._json({}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        path = urlparse(self.path).path

        if path == "/api/mail/recap/seen":
            RECAP_SEEN.append("2026-09-16T12:00:00+00:00")
            return self._json({"since": RECAP_SEEN[-1]})
        fb = re.match(r"^/api/mail/([^/]+)/feedback$", path)
        if fb:
            try:
                verdict = (json.loads(raw or b"{}") or {}).get("verdict")
            except ValueError:
                verdict = None
            if verdict in ("done", "not_relevant", "snoozed"):
                DIGEST_SETTLED.add(fb.group(1))
            return self._json({"ok": True, "verdict": verdict})
        if path == "/api/ai/consent":
            CONSENT_POSTS.append(json.loads(raw or b"{}"))
            CONSENT["state"] = "granted"
            return self._json(consent_status())
        if path == "/api/compose/draft":
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = {}
            return self._json({"token": "stub-token",
                               "summary": compose_summary(body),
                               "transport": dict(TRANSPORT)})
        if path.startswith("/api/compose/") and path.endswith("/send"):
            handoff = TRANSPORT["mode"] == "hands_off"
            return self._json({
                "token": "stub-token", "already": False,
                "sent_at": "2026-09-20T12:00:00+00:00",
                "summary": {}, "outcome": {
                    "delivered": not handoff,
                    "handoff": "Drafts" if handoff else "",
                    "sent_copy": "appended" if not handoff else "skipped",
                    "detail": ("Saved to Drafts. Open it in your mail client and "
                               "press send.") if handoff else "",
                    "recipients": ["careers@example.edu"], "refused": [],
                    "message_id": "<x@example.edu>",
                }})

        return self._json(self._route() or {"ok": True})

    do_PATCH = do_POST
    do_DELETE = do_POST


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
