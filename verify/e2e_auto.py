#!/usr/bin/env python3
"""Reply A -> B on a fresh install, with no sending settings at all.

The claim under test is the user's own model of sending: "I am reading mail
sent to A; I answer B; it comes from A." So nothing in Settings is touched.
The mail_transport setting is never written, which makes "automatic" the
fresh-install default rather than something this script chose.

  .emlx on disk  ->  backend on :8765  ->  production bundle in Chromium
  ->  reply bar  ->  review  ->  Send  ->  asked for A's password, once
  ->  a wrong one is refused and not kept  ->  the right one sends the SAME
  approved message  ->  SMTP on a socket  ->  a copy filed in Sent over IMAP.

One thing is seeded rather than discovered: where A's servers are. Discovery
(autoconfig/ISPDB/MX) needs the internet and is covered by test_autoconfig.py;
here the discovered result is stored exactly as discovery would store it,
pointing at local servers.

Run:  FG_BACKEND=backend python3 verify/e2e_auto.py      Exit 0 only if every claim holds.
"""
from __future__ import annotations

import email
import email.policy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_drafts as base                                          # noqa: E402
from e2e_drafts import (BACKEND, ORIGINAL, PARENT_ID, PORT, ROOT_ID, SHOTS,  # noqa: E402
                        USER, Claims, api, emlx, wait_for_backend)
from fake_imap import FakeIMAP                                     # noqa: E402
from fake_smtp import FakeSMTP                                     # noqa: E402

PASSWORD = "a-real-password-7"
TYPED = "Registering now for the 20th, thank you."


def main() -> int:
    claim = Claims()
    SHOTS.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="fg-e2e-auto-"))
    store = home / "Mail" / "V10" / "ACCOUNT" / "INBOX.mbox" / "Messages"
    store.mkdir(parents=True)
    (store / "101.emlx").write_bytes(emlx(ORIGINAL))

    imap = FakeIMAP(username=USER, password=PASSWORD, persistent=True,
                    mailboxes=[("\\HasNoChildren", "INBOX"),
                               ("\\HasNoChildren \\Sent", "Sent Items"),
                               ("\\HasNoChildren \\Drafts", "Drafts")])
    imap.start(); imap.ready.wait(5)
    smtp = FakeSMTP()
    smtp.start(); smtp.ready.wait(5)
    smtp_user_ok = [False]

    env = {**os.environ, "FOOLSGOLD_HOME": str(home / "state"), "FOOLSGOLD_PORT": str(PORT)}
    seed = subprocess.run([sys.executable, "-c", (
        "from app import db; db.init_db();"
        "from app.transports import accounts;"
        "from app.transports.autoconfig import ServerConfig;"
        "db.set_setting('mail_source','applemail');"
        f"db.set_setting('applemail_root', r'{home / 'Mail' / 'V10'}');"
        "db.set_setting('applemail_inbox_only','false');"
        f"db.set_setting('user_address','{USER}');"
        "db.set_setting('llm_provider','none');"
        "db.set_setting('theme','gold'); db.set_setting('ui_language','ko');"
        f"accounts.remember_config('{USER}', ServerConfig('127.0.0.1', {imap.port}, 'plain',"
        f" '127.0.0.1', {smtp.port}, 'plain', '{USER}', '{USER}', 'e2e'))")],
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
        current = api("/api/transports").get("current")
        claim(current == "auto", "1. a fresh install sends automatically; nothing was configured",
              f"current={current!r}")
        api("/api/mail/sync", "POST")
        try:
            _browser_flow(claim, imap, smtp)
        except Exception as exc:                                   # noqa: BLE001
            claim(False, "the UI flow completed",
                  f"{type(exc).__name__}: {str(exc).splitlines()[0]}")
        _server_claims(claim, imap, smtp, home)
    finally:
        backend.terminate()
        try:
            backend.wait(timeout=10)
        except subprocess.TimeoutExpired:
            backend.kill()
        imap.stop()
        shutil.rmtree(home, ignore_errors=True)
    return claim.report()


def _browser_flow(claim, imap, smtp) -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_default_timeout(8000)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
            page.wait_for_timeout(800)
            page.click("button[data-view='priority']")
            page.wait_for_timeout(500)
            page.click(".mail-item:has-text('ICAC')")
            page.wait_for_timeout(600)
            page.click(".reply-bar [data-action='reply']")
            page.wait_for_timeout(400)
            page.fill(".compose-body", TYPED)
            page.click(".compose-foot .btn.primary")
            page.wait_for_selector(".compose-review", timeout=10000)
            frm = page.evaluate("() => document.querySelector(\"dd[data-field='from']\").textContent")
            button = page.evaluate("() => document.querySelector('.compose-foot .btn.primary').textContent")
            claim(USER in frm and "보내기" in button and not page.query_selector(".compose-note.handoff"),
                  "2. the review sends from the address the mail came to, and says Send",
                  f"from={frm!r} button={button!r}")

            page.click(".compose-foot .btn.primary")
            page.wait_for_selector(".compose-unlock", timeout=10000)
            asked = page.evaluate("""() => { const u = document.querySelector('.compose-unlock');
                return { addr: u.dataset.unlock, text: u.innerText }; }""")
            claim(asked["addr"] == USER and USER in asked["text"],
                  "3. the first send asks for that address's password, in place",
                  json.dumps(asked, ensure_ascii=False))
            base._korean(claim, "3b. the question is in the UI's language",
                         asked["text"].replace(USER, ""))
            claim(smtp.payload == b"" and not imap.appended,
                  "4. asking sent nothing", f"smtp={len(smtp.payload)} imap={len(imap.appended)}")
            page.screenshot(path=str(SHOTS / "e2e-auto-1-asked.png"))

            page.fill(".compose-unlock input[type='password']", "wrong-password")
            page.click("[data-action='unlock-send']")
            page.wait_for_selector(".compose-unlock .compose-error", timeout=10000)
            summary = api("/api/transports")["statuses"]["auto"]["accounts"]
            claim(not any(a["ready"] for a in summary) and smtp.payload == b"",
                  "5. a wrong password is refused, not kept, and sends nothing",
                  json.dumps(summary))
            page.screenshot(path=str(SHOTS / "e2e-auto-2-rejected.png"))

            page.fill(".compose-unlock input[type='password']", PASSWORD)
            page.click("[data-action='unlock-send']")
            page.wait_for_selector(".compose-done-line", timeout=20000)
            done = page.evaluate("() => document.querySelector('.compose-done').innerText")
            claim("보냈습니다" in done and not page.query_selector("[data-fallback]"),
                  "6. with the right password the same message is sent, and it says so", done)
            page.screenshot(path=str(SHOTS / "e2e-auto-3-sent.png"))

            page.click(".compose-foot .btn.primary")
            page.click("button[data-view='settings']")
            page.wait_for_timeout(400)
            page.click(".settings-tabs .chip:has-text('보내기')")
            page.wait_for_timeout(500)
            shown = page.evaluate("""() => ({
                transport: document.querySelector("select[data-key='mail_transport']")?.value,
                fields: document.querySelectorAll("input[data-key='imap_host'], input[data-key='smtp_password']").length,
                accounts: document.querySelector('.send-accounts')?.innerText || '' })""")
            claim(shown["transport"] == "auto" and shown["fields"] == 0 and USER in shown["accounts"],
                  "7. Settings asks nothing and shows the address it can now send from",
                  json.dumps(shown, ensure_ascii=False))
            page.screenshot(path=str(SHOTS / "e2e-auto-4-settings.png"))
            claim(not errors, "8. no uncaught error in the page", "\n".join(errors))
        finally:
            browser.close()


def _server_claims(claim, imap, smtp, home) -> None:
    smtp.join(timeout=5)
    if not claim(smtp.payload, "9. the message reached the SMTP server"):
        return
    msg = email.message_from_bytes(smtp.payload, policy=email.policy.default)
    claim(smtp.mail_from == USER and "careers@example.edu" in smtp.rcpt_to
          and smtp.credentials == (USER, PASSWORD),
          "10. A -> B: envelope from the reading address, to the sender, with its own login",
          f"from={smtp.mail_from!r} rcpt={smtp.rcpt_to} creds={smtp.credentials}")
    claim(msg["In-Reply-To"] == PARENT_ID and ROOT_ID in (msg["References"] or ""),
          "11. it threads", f"In-Reply-To={msg['In-Reply-To']!r}")
    claim(TYPED in msg.get_body(("plain",)).get_content(), "12. what was typed is what went")
    sent = [a for a in imap.appended if a["mailbox"] == "Sent Items"]
    claim(len(sent) == 1 and len(imap.appended) == 1 and TYPED.encode() in sent[0]["message"],
          "13. one copy filed in the account's own Sent folder, found by its flag",
          f"appended={[a['mailbox'] for a in imap.appended]} "
          f"outcome={api('/api/compose/' + base._last_token(home)).get('outcome')}")


if __name__ == "__main__":
    sys.exit(main())
