#!/usr/bin/env python3
"""Write a reply in the real app and watch it arrive in a real IMAP Drafts folder.

Every layer this app owns runs for real, as separate processes and sockets:

  .emlx on disk  ->  Apple Mail source sync  ->  the backend on :8765
  ->  the production bundle in Chromium  ->  Settings, typed into like a person
  ->  compose, review, approve  ->  ImapDraftTransport  ->  IMAP APPEND
  ->  an IMAP server on a socket, which records what it was given.

Two things are seeded rather than typed, because they are not under test: the
mail-store path and the user's own address (the Mail source section). The
Sending section -- the thing this proves -- is filled in through the UI.

What this cannot prove: that YOUR provider accepts the login. The server here
speaks plain IMAP on localhost; a real one needs TLS and real credentials, and
that last hop is yours to run from the finished Settings screen.

Run:  python3 verify/e2e_drafts.py        Exit 0 only if every claim holds.
"""
from __future__ import annotations

import email
import email.policy
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
BACKEND = Path(os.environ.get("FG_BACKEND", "/home/claude/fg/backend"))
sys.path.insert(0, str(BACKEND / "tests"))
from fake_imap import FakeIMAP                                    # noqa: E402

# Overridable, and the mutation driver overrides it. A mutant writing into the
# same folder as the real run left a screenshot of ITS broken form where the
# real one belonged -- and that stale picture was very nearly "fixed".
SHOTS = Path(os.environ.get("FG_SHOTS", ROOT / "verify" / "shots"))
PORT = 8765
USER, PASSWORD = "danny@example.edu", "app-password-123"
PARENT_ID = "<icac-reminder-17@careers.example.edu>"
ROOT_ID = "<icac-announce-08@careers.example.edu>"
TYPED = "Registering now for the 20th — thank you."

ORIGINAL = (
    "From: Career Development Programs <careers@example.edu>\r\n"
    f"To: Danny Park <{USER}>\r\n"
    "Subject: =?utf-8?q?=E2=80=9CEthics_in_Practice=E2=80=9D?= ICAC Seminar\r\n"
    "Date: Thu, 17 Sep 2026 09:00:00 +0800\r\n"
    f"Message-ID: {PARENT_ID}\r\n"
    f"References: {ROOT_ID}\r\n"
    "MIME-Version: 1.0\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "The seminar is on 20 September. Please register in advance.\r\n"
).encode("utf-8")


def emlx(raw: bytes) -> bytes:
    """Apple Mail's container: a byte count, the message, then a plist."""
    plist = (b'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>'
             b"<key>flags</key><integer>0</integer></dict></plist>\n")
    return str(len(raw)).encode() + b"\n" + raw + plist


class Claims:
    def __init__(self):
        self.rows: list[tuple[bool, str, str]] = []

    def __call__(self, ok, claim, detail=""):
        self.rows.append((bool(ok), claim, str(detail)))
        return bool(ok)

    def report(self) -> int:
        width = max(len(c) for _, c, _ in self.rows) + 2
        print("-" * (width + 8))
        for ok, claim, detail in self.rows:
            print(f"{claim:<{width}} {'PASS' if ok else 'FAIL'}")
            if detail and not ok:
                for line in detail.splitlines()[:8]:
                    print(f"    {line}")
        print("-" * (width + 8))
        passed = sum(ok for ok, _, _ in self.rows)
        print(f"claims: {passed}/{len(self.rows)}")
        return 0 if passed == len(self.rows) else 1


def api(path, method="GET", body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def wait_for_backend(timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            api("/api/health")
            return True
        except Exception:                                          # noqa: BLE001
            time.sleep(0.3)
    return False


def main() -> int:
    claim = Claims()
    SHOTS.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="fg-e2e-"))
    store = home / "Mail" / "V10" / "ACCOUNT" / "INBOX.mbox" / "Messages"
    store.mkdir(parents=True)
    (store / "101.emlx").write_bytes(emlx(ORIGINAL))

    global _IMAP
    imap = _IMAP = FakeIMAP(username=USER, password=PASSWORD, persistent=True)
    imap.start()
    imap.ready.wait(timeout=5)

    # Seed only what is not under test, before the backend starts.
    env = {**os.environ, "FOOLSGOLD_HOME": str(home / "state"), "FOOLSGOLD_PORT": str(PORT)}
    seed = subprocess.run([sys.executable, "-c", (
        "from app import db; db.init_db();"
        "db.set_setting('mail_source','applemail');"
        f"db.set_setting('applemail_root', r'{home / 'Mail' / 'V10'}');"
        "db.set_setting('applemail_inbox_only','false');"
        f"db.set_setting('user_address','{USER}');"
        "db.set_setting('llm_provider','none');"
        "db.set_setting('theme','gold'); db.set_setting('ui_language','ko')")],
        cwd=BACKEND, env=env, capture_output=True, text=True)
    if seed.returncode:
        print(seed.stderr)
        return 2

    backend = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--log-level", "warning"],
        cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        if not claim(wait_for_backend(), "0. the real backend started on :8765"):
            return claim.report()

        synced = api("/api/mail/sync", "POST")
        subjects = [m["subject"] for m in api("/api/mail?sort=priority").get("items", [])]
        if not claim(any("ICAC" in x for x in subjects),
                     "1. the .emlx on disk was synced into the mail list",
                     f"sync={synced} subjects={subjects}"):
            return claim.report()

        try:
            _browser_flow(claim)
        except Exception as exc:                                   # noqa: BLE001
            # A harness that fails by crashing says THAT something broke and
            # not WHAT. The first mutant to reach this path -- a Save button
            # left disabled -- surfaced as a 30-second Playwright timeout and a
            # stack trace. Name the step instead, and still report.
            claim(False, "the UI flow completed",
                  f"{type(exc).__name__}: {str(exc).splitlines()[0]}")

        _server_claims(claim, imap, home)
    finally:
        backend.terminate()
        try:
            backend.wait(timeout=10)
        except subprocess.TimeoutExpired:
            backend.kill()
        imap.stop()
        shutil.rmtree(home, ignore_errors=True)
    return claim.report()


def _settings_tab(page) -> None:
    page.click("button[data-view='settings']")
    page.wait_for_timeout(400)
    page.click(".settings-tabs .chip:has-text('보내기')")
    page.wait_for_timeout(300)


def _browser_flow(claim) -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        # Short, so a stuck step fails in seconds with a name, not in thirty.
        page.set_default_timeout(8000)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
            page.wait_for_timeout(800)

            # -------------------------------------------- Settings, by hand
            _settings_tab(page)
            page.select_option("select[data-key='mail_transport']", "imap_draft")
            page.fill("input[data-key='user_name']", "Danny Park")
            page.fill("input[data-key='imap_host']", "127.0.0.1")
            page.select_option("select[data-key='imap_security']", "plain")
            page.fill("input[data-key='imap_port']", str(_imap_port()))
            page.fill("input[data-key='imap_username']", USER)
            page.fill("input[data-key='smtp_password']", PASSWORD)
            page.screenshot(path=str(SHOTS / "e2e-1-settings.png"))

            page.click("button[data-action='test-send']")
            page.wait_for_selector(".send-result", timeout=15000)
            result = page.evaluate("""() => { const r = document.querySelector('.send-result');
                                      return { ok: r.dataset.result, text: r.textContent }; }""")
            claim(result["ok"] == "ok" and "Drafts" in result["text"],
                  "2. 'Save and test' signs in and names the Drafts folder it found",
                  json.dumps(result, ensure_ascii=False))
            _korean(claim, "2b. the test result is in the UI's language", result["text"])
            claim(not _IMAP.appended, "3. the connection test wrote nothing to the server",
                  f"appended={len(_IMAP.appended)}")
            claim(_IMAP.logins and _IMAP.logins[-1] == (USER, PASSWORD),
                  "4. the password typed into the form is the one the server received",
                  f"logins={_IMAP.logins}")
            page.screenshot(path=str(SHOTS / "e2e-2-tested.png"))

            page.reload(wait_until="networkidle")
            page.wait_for_timeout(500)
            _settings_tab(page)
            field = page.evaluate(
                "() => document.querySelector(\"input[data-key='smtp_password']\").value")
            claim(field == "", "5. a saved password is never put back into the field",
                  f"field value={field!r}")

            # Claim 6 used to re-save this form and check the password still
            # worked. It could not fail: the form only submits fields you have
            # edited, so the mask was never sent, and removing the server's
            # guard changed nothing it could see. The guard exists for a
            # client that DOES round-trip every field -- so be that client.
            shown = api("/api/settings")
            api("/api/settings", "PATCH", {"values": {"smtp_password": shown.get("smtp_password")}})
            again = api("/api/transports/test", "POST", {})
            claim(shown.get("smtp_password") == "********" and again.get("ok"),
                  "6. a client that round-trips the masked secret cannot overwrite it",
                  f"served={shown.get('smtp_password')!r} then test={json.dumps(again)}")

            # -------------------------------------------- compose, by hand
            page.click("button[data-view='priority']")
            page.wait_for_timeout(500)
            page.click(".mail-item:has-text('ICAC')")
            page.wait_for_timeout(600)
            page.click(".reply-bar [data-action='reply']")                    # 답장
            page.wait_for_timeout(400)
            page.fill(".compose-body", TYPED)
            page.screenshot(path=str(SHOTS / "e2e-3-compose.png"))
            page.click(".compose-foot .btn.primary")              # 확인하기
            page.wait_for_selector(".compose-review", timeout=10000)
            review = page.evaluate("""() => ({
                to: document.querySelector("dd[data-field='to']")?.textContent || '',
                from: document.querySelector("dd[data-field='from']")?.textContent || '',
                subject: document.querySelector("dd[data-field='subject']")?.textContent || '',
                text: document.querySelector('.compose-preview')?.textContent || '',
                button: document.querySelector('.compose-foot .btn.primary')?.textContent.trim() || '',
                warned: !!document.querySelector('.compose-note.handoff'),
            })""")
            claim("careers@example.edu" in review["to"] and "Danny Park" in review["from"]
                  and review["subject"].startswith("Re:"),
                  "7. the review shows the server-built headers: derived To, From, Re:",
                  json.dumps(review, ensure_ascii=False))
            claim(TYPED in review["text"] and "> The seminar is on 20 September" in review["text"],
                  "8. the review shows what was typed and the quoted original, read off disk",
                  review["text"])
            claim(review["warned"] and "보내기" not in review["button"],
                  "9. the button says 'save to Drafts', not 'send', and warns before the click",
                  f"button={review['button']!r} warned={review['warned']}")
            page.screenshot(path=str(SHOTS / "e2e-4-review.png"))

            page.click(".compose-foot .btn.primary")              # 임시보관함에 저장
            page.wait_for_selector(".compose-done-line", timeout=15000)
            done = page.evaluate("() => document.querySelector('.compose-done-line').textContent")
            claim("Drafts" in done, "10. the app says where the message went, not that it was sent",
                  done)
            whole = page.evaluate("() => document.querySelector('.compose-done').innerText")
            _korean(claim, "10b. the done screen says it once, in the UI's language", whole)
            page.screenshot(path=str(SHOTS / "e2e-5-saved.png"))
            claim(not errors, "11. no uncaught error in the page", "\n".join(errors))
        finally:
            try:
                page.screenshot(path=str(SHOTS / "e2e-last.png"))
            except Exception:                                      # noqa: BLE001
                pass
            browser.close()


_HANGUL = re.compile(r"[\uac00-\ud7a3]")
# A run of four or more English words is prose, not a folder name or an address.
_ENGLISH_PROSE = re.compile(r"(?:\b[A-Za-z]{2,}\b[ ,.'—-]+){4,}")


def _korean(claim, label, text) -> None:
    """The server speaks English. Anything it says that reaches a Korean
    screen verbatim is a sentence the user may not be able to read -- and the
    render check cannot see it, because it only knows the strings in i18n.js."""
    prose = _ENGLISH_PROSE.search(text or "")
    claim(bool(_HANGUL.search(text or "")) and not prose, label,
          f"text={text!r}" + (f"\nEnglish prose: {prose.group(0)!r}" if prose else ""))


def _server_claims(claim, imap, home) -> None:
    if not claim(len(imap.appended) == 1, "12. exactly one message reached the server",
                 f"appended={len(imap.appended)}"):
        return
    got = imap.appended[0]
    msg = email.message_from_bytes(got["message"], policy=email.policy.default)
    body = msg.get_body(("plain",)).get_content()
    claim(got["mailbox"] == "Drafts" and "\\Draft" in got["flags"],
          "13. it was filed in Drafts with the \\Draft flag",
          f"mailbox={got['mailbox']!r} flags={got['flags']!r}")
    claim(msg["In-Reply-To"] == PARENT_ID and ROOT_ID in (msg["References"] or "")
          and PARENT_ID in (msg["References"] or ""),
          "14. it threads: In-Reply-To and References from the original file",
          f"In-Reply-To={msg['In-Reply-To']!r} References={msg['References']!r}")
    claim(msg["Subject"] == "Re: “Ethics in Practice” ICAC Seminar"
          and "careers@example.edu" in msg["To"]
          and "Danny Park" in msg["From"] and USER in msg["From"],
          "15. it is addressed: To the sender, From the user's name and address",
          f"From={msg['From']!r} To={msg['To']!r} Subject={msg['Subject']!r}")
    claim(TYPED in body, "16. what the user typed is what arrived", body[:200])
    stored = api(f"/api/compose/{_last_token(home)}")
    claim(stored.get("sent_at") and stored["outcome"]["delivered"] is False,
          "17. the record says handed off, not delivered",
          json.dumps(stored.get("outcome"), ensure_ascii=False))


# The IMAP server is shared with the browser flow's claims about what it saw.
_IMAP: FakeIMAP | None = None


def _imap_port() -> int:
    return _IMAP.port


def _last_token(home: Path) -> str:
    import sqlite3
    conn = sqlite3.connect(home / "state" / "foolsgold.db")
    row = conn.execute("SELECT token FROM outgoing ORDER BY created_at DESC LIMIT 1").fetchone()
    return row[0] if row else ""


if __name__ == "__main__":
    sys.exit(main())
