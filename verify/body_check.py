#!/usr/bin/env python3
"""The reading pane's sanitiser, asserted in a real browser.

`prepareBody` runs in the renderer and uses DOMParser, so it cannot be tested
in Python or in node without a DOM. This drives the real module in Chromium
through the built bundle's source, which is the only place its behaviour is
the behaviour that ships.

Every case here is either a bug that was on screen (cid images, dead links) or
an attack the mail-client literature documents (script injection, tracking
pixels, javascript: URLs, CSS background beacons).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
MODULE = ROOT / "frontend" / "src" / "lib" / "mailBody.js"

CASES = [
    # (name, html, showRemote, assertions on the result)
    ("a cid: image is rewritten to the local backend",
     '<img src="cid:logo@fools.gold">', False,
     lambda r: "/api/mail/e1/part/logo%40fools.gold" in r["html"] and "cid:" not in r["html"]),

    ("a cid: image keeps working when it carries angle brackets",
     '<img src="cid:<logo@x>">', False,
     lambda r: "/api/mail/e1/part/logo%40x" in r["html"]),

    ("a remote image is blocked and counted",
     '<img src="https://tracker.example/pixel.gif?u=me">', False,
     lambda r: r["remoteBlocked"] == 1 and "tracker.example" not in r["html"].split("data-blocked")[0]),

    ("a remote image loads once the user asks",
     '<img src="https://cdn.example/banner.png">', True,
     lambda r: 'src="https://cdn.example/banner.png"' in r["html"]),

    ("a CSS background beacon is neutralised too",
     '<div style="background-image:url(https://tracker.example/p.gif)">x</div>', False,
     lambda r: "tracker.example" not in r["html"] and r["remoteBlocked"] == 1),

    ("script is removed",
     '<p>hi</p><script>fetch("//evil")</script>', False,
     lambda r: "<script" not in r["html"].lower() and "hi" in r["html"]),

    ("a nested iframe is removed",
     '<iframe src="https://evil.example"></iframe>', False,
     lambda r: "<iframe" not in r["html"].lower()),

    ("event handlers are stripped wherever they sit",
     '<div onmouseover="steal()"><b onclick="x()">t</b></div>', False,
     lambda r: "onmouseover" not in r["html"].lower() and "onclick" not in r["html"].lower()),

    ("a javascript: link loses its href",
     '<a href="javascript:alert(1)">click</a>', False,
     lambda r: "javascript:" not in r["html"].lower()),

    ("a javascript: link split by whitespace still loses its href",
     '<a href="java\tscript:alert(1)">click</a>', False,
     lambda r: "alert(1)" not in r["html"]),

    ("a real link is opened outside the app",
     '<a href="https://uni.edu/apply">apply</a>', False,
     lambda r: 'target="_blank"' in r["html"] and "noopener" in r["html"]),

    ("mailto survives, because a third of the links in this mailbox are mailto",
     '<a href="mailto:prof@uni.edu">mail</a>', False,
     lambda r: 'href="mailto:prof@uni.edu"' in r["html"] and 'target="_blank"' in r["html"]),

    ("a form cannot be submitted from a mail body",
     '<form action="https://evil.example"><input name="p"></form>', False,
     lambda r: "<form" not in r["html"].lower() and "<input" not in r["html"].lower()),
]


def main() -> int:
    src = MODULE.read_text()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto("about:blank")
        page.add_script_tag(content=src.replace("export function", "function")
                            .replace("export const", "const"))
        failures = []
        for name, html, remote, check in CASES:
            result = page.evaluate(
                "([h, r]) => prepareBody(h, {emailId: 'e1', apiBase: '', showRemote: r})",
                [html, remote])
            try:
                ok = check(result)
            except Exception as exc:                      # noqa: BLE001
                ok, result = False, {"error": str(exc), **result}
            print(f"  {'ok ' if ok else 'BAD'} {name}")
            if not ok:
                failures.append((name, json.dumps(result)[:220]))

        # The frame document must name the backend in img-src, or the CSP that
        # keeps everything else out also blocks the images this exists to show.
        doc = page.evaluate(
            "() => frameDocument('<p>x</p>', {apiBase: 'http://127.0.0.1:8765', showRemote: false})")
        for must in ("default-src 'none'", "img-src http://127.0.0.1:8765",
                     "<base target=\"_blank\">"):
            ok = must in doc
            print(f"  {'ok ' if ok else 'BAD'} frame document contains {must!r}")
            if not ok:
                failures.append(("frame document", must))
        browser.close()

    print("-" * 66)
    print(f"body checks: {len(CASES) + 3 - len(failures)}/{len(CASES) + 3}")
    for name, detail in failures:
        print(f"   FAILED {name}: {detail}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
