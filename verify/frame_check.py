#!/usr/bin/env python3
"""Motion, asserted — so a claim about smoothness is a command you can run.

Why this file exists: every performance claim in this project was, until now, a
sentence in a document. `Instrumented Innocence` from the ontology vault is the
general form of the failure -- a system reports clean on every instrument you
have, because the instruments cover the layer that is not broken. Sentences about frame rate are unfalsifiable by the
person reading them, and the session that produced them had already been wrong
three times. This measures the four things that actually decide whether a
completion animation looks smooth, and exits 1 if any of them regress.

Run it:  python3 verify/frame_check.py          (needs playwright)

Claim 0 is the one to read first. It reproduces the measurement mistake that
sent this work down two dead ends: a PerformanceObserver opened with
`buffered: true` replays the page-load frame, so a 240ms frame from *mount* is
delivered as though it belonged to the click you just made. Both observers run
on the same page, on the same click, and print their disagreement.
"""
from __future__ import annotations

import os
import re
import subprocess
import statistics
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PORT = 8765
ROWS = "180"          # plus the seven fixture rows; seven alone hides everything

# Paint- and layout-bound properties. A keyframe touching one of these cannot
# run on the compositor: it repaints (or relayouts) the element every frame,
# which is invisible to script timing and very visible to the eye.
PAINT = ("background-color", "background-position", "box-shadow", "border-color")
LAYOUT = ("max-height", "min-height", "padding", "margin", "top:", "left:", "bottom:", "right:")
# Keyframes used by the completion gesture. These are the ones that must stay
# clean; `burn-heat` (muting) and `shimmer` (skeleton) are allowed their paint.
COMPLETION = {"done-tint", "ok-ring", "done-slide-left", "done-out", "strike",
              "tick-draw", "box-pop", "ripple", "tick-glint", "lift-out", "fade-up"}

PROBE = """
() => new Promise(res => {
  const out = { buffered: [], gated: [] };
  new PerformanceObserver(l => { for (const e of l.getEntries())
      if (e.duration >= 50) out.buffered.push({ ms: Math.round(e.duration), at: Math.round(e.startTime) });
  }).observe({ type: 'long-animation-frame', buffered: true });

  let t0 = 0;
  new PerformanceObserver(l => { for (const e of l.getEntries())
      if (e.startTime >= t0 && e.duration >= 50)
        out.gated.push({ ms: Math.round(e.duration), at: Math.round(e.startTime) });
  }).observe({ type: 'long-animation-frame' });

  setTimeout(() => {
    t0 = performance.now();
    out.clickAt = Math.round(t0);
    out.animsBefore = document.getAnimations().length;
    const c = document.querySelector('.mail-item .row-check');
    if (!c) { out.error = 'no mail rows rendered'; return res(out); }
    c.click();
    setTimeout(() => {
      out.rows = document.querySelectorAll('.mail-item').length;
      out.animsAfter = document.getAnimations().length;
      res(out);
    }, 2200);
  }, 600);
})
"""


class Report:
    def __init__(self):
        self.rows = []

    def check(self, ok, claim, detail=""):
        self.rows.append((bool(ok), claim, detail))

    def render(self):
        print("-" * 68)
        for ok, claim, detail in self.rows:
            print(f"{claim:<52} {'PASS' if ok else 'FAIL'}")
            if detail:
                for line in str(detail).splitlines():
                    print(f"    {line}")
        good = sum(1 for ok, _, _ in self.rows if ok)
        print("-" * 68)
        print(f"claims: {good}/{len(self.rows)}")
        return good == len(self.rows)


def keyframe_audit(css_path: Path):
    """Which keyframes animate something that cannot be composited."""
    css = css_path.read_text()
    out = {}
    for m in re.finditer(r"@keyframes\s+([\w-]+)\s*\{", css):
        name, i, depth = m.group(1), m.end(), 1
        while depth and i < len(css):
            depth += (css[i] == "{") - (css[i] == "}")
            i += 1
        body = css[m.end():i - 1]
        hits = [p for p in PAINT + LAYOUT if p in body]
        if hits:
            out[name] = hits
    return out


def main() -> int:
    r = Report()

    dirty = keyframe_audit(ROOT / "frontend" / "src" / "styles.css")
    offenders = sorted(set(dirty) & COMPLETION)
    r.check(not offenders,
            "3. no completion keyframe animates paint or layout",
            "\n".join(f"{k}: {dirty[k]}" for k in offenders) or
            "the gesture runs on transform and opacity only; " +
            f"{len(dirty)} other keyframes still paint: {sorted(dirty)}")

    env = dict(os.environ, FG_BULK=ROWS)
    server = subprocess.Popen(
        [sys.executable, str(ROOT / "verify" / "stub_api.py"), str(PORT), "ko", "dark"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.request.get(f"http://127.0.0.1:{PORT}/__set?theme=dark&lang=ko")
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
            page.wait_for_timeout(1800)

            # --- presented frames, which is the only thing the user sees ------
            # Script timing reported this animation as free while it was
            # visibly dropping frames, because paint and raster are not script.
            cdp = page.context.new_cdp_session(page)
            stamps = []

            def on_frame(ev):
                stamps.append((ev.get("metadata") or {}).get("timestamp") or time.time())
                try:
                    cdp.send("Page.screencastFrameAck", {"sessionId": ev["sessionId"]})
                except Exception:
                    pass

            cdp.on("Page.screencastFrame", on_frame)
            cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 10,
                                              "everyNthFrame": 1, "maxWidth": 640, "maxHeight": 450})
            page.wait_for_timeout(400)
            stamps.clear()
            page.evaluate("()=>document.querySelector('.mail-item .row-check').click()")
            page.wait_for_timeout(900)          # the exit finishes at ~760ms
            cdp.send("Page.stopScreencast")
            gaps = [(stamps[i + 1] - stamps[i]) * 1000 for i in range(len(stamps) - 1)]
            worst = max(gaps) if gaps else 0.0
            r.check(gaps and worst < 150,
                    "2. no visible freeze while the animation plays",
                    f"{len(stamps)} frames presented, worst gap {worst:.0f}ms "
                    f"(median {statistics.median(gaps):.0f}ms). It was 280ms mid-animation.\n"
                    "Coarse on purpose: this is the metric that caught the original "
                    "defect, but headless software compositing flatters it, so it "
                    "catches a gross regression and not a small one. Claims 1 and 3 "
                    "are the sensitive ones -- both fail if their fix is reverted, "
                    "which was tested by reverting it.")

            page.reload(wait_until="networkidle")
            page.wait_for_timeout(1800)
            out = page.evaluate(PROBE)
            browser.close()
    finally:
        server.terminate()

    if out.get("error"):
        r.check(False, "0. the list rendered at all", out["error"])
        r.render()
        return 1

    r.check(not out["gated"],
            "0. the tick costs no long animation frame",
            f"click at t={out['clickAt']}ms over {out['rows']} rows\n"
            f"timestamp-gated observer: {out['gated'] or 'nothing >= 50ms'}\n"
            f"buffered:true observer:   {out['buffered']}  <- page-load frames, "
            "replayed. Reading these as the click's cost is the mistake this "
            "claim exists to prevent.")

    r.check(out["animsBefore"] == 0 and out["animsAfter"] == 0,
            "1. no animation is retained after it finishes",
            f"{out['animsBefore']} live before the tick, {out['animsAfter']} after, "
            f"over {out['rows']} rows. It was 375: every entrance used a forwards "
            "fill, so finished animations were ticked forever.")

    ok = r.render()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
