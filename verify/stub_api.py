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
        "rationale": "", "source": "llm", "explored": 0,
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
         bucket="action", score=120, reason_code="direct"),
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
                    "ai": {"available": True, "off": False, "detail": ""}},
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
        "day": "2026-09-16", "headline": "오늘 마감 1건, 이번 주 2건.",
        "items": [
            {"id": "m2", "note": "17일 워크숍, Career Center 등록 여부 바로 확인하세요.",
             "subject": "Professional Grooming for All", "sender": "Career Development Programs",
             "deadline": "2026-09-16", "done": False},
            {"id": "m1", "note": "18일 밤 로봇팀 설명회, 참석 가능하면 신청하세요.",
             "subject": "[Tonight - Info Session] Recruitment of the HKUST Robotics Team",
             "sender": "Center for Global & Community Engagement",
             "deadline": "2026-09-18", "done": False},
            {"id": "m3", "note": "진행 보고서 제출, 어제까지였습니다.",
             "subject": "Final Reminder: submit the progress report",
             "sender": "Course Office", "deadline": "2026-09-15", "done": False},
        ],
        "model": "ollama:qwen3.5:4b", "created_at": "2026-09-16T08:00:00+00:00"},
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
            return self._json({"theme": SETTINGS["theme"], "lang": SETTINGS["ui_language"]})
        if self.path.startswith("/api/mail?") or self.path == "/api/mail":
            special = self._mail_query()
            if special is not None:
                return self._json(special)
        if self.path.startswith("/api/"):
            payload = self._route()
            return self._json(payload if payload is not None else {}, 200 if payload else 404)
        return super().do_GET()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        return self._json(self._route() or {"ok": True})

    do_PATCH = do_POST
    do_DELETE = do_POST


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
