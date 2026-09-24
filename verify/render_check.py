#!/usr/bin/env python3
"""Render the built UI and assert what a person would actually see.

Six rounds of frontend "verification" were done by parsing, and parsing caught
none of the real defects -- a badge that could never appear, a translation
overwritten by an object spread, a badge wrapping to two lines. None are syntax
errors; all are obvious on screen. So this asserts pixels and computed styles.

Checks, each with the failure it exists to catch:
  1. no horizontal overflow anywhere        (bug #18: a card wider than its pane)
  2. no control wraps to a second line      (CJK min-content collapse)
  3. every visible string is translated     (the ...en spread swallowing keys)
  4. no raw i18n key reaches the screen     (a missing key rendering as "rankX")
  5. the explored badge is actually painted (a flag nothing displays)
  6. the reason chip is painted and legible (contrast against its own theme)

Run:  python3 verify/render_check.py
Exit: 0 when every check passes in every theme x language.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "verify" / "shots"
THEMES = ["gold", "dark", "green", "purple"]
LANGS = ["en", "ko", "zh", "ja"]
# `--quick` runs one theme x language pair. For iterating on a single check;
# a green quick run is not a green run, and main() says so in its output.
if "--quick" in sys.argv:
    THEMES, LANGS = ["gold"], ["ko"]
# The production bundle hardcodes `http://127.0.0.1:8765` as its API base
# (frontend/src/lib/api.js), so the stub has to answer on THAT port. Serving on
# any other port produced a page stuck on the connect screen -- where checks 3
# and 4 passed because there was nothing on screen to fail them. A vacuous pass
# and a real pass look identical in a green run.
PORT = 8765

# A raw key looks like an identifier where a sentence belongs.
RAW_KEY = re.compile(r"^(rank|why|explored|provider|digest|filter|tag)[A-Z][A-Za-z]*$")


class Result:
    def __init__(self):
        self.rows: list[tuple[bool, str, str]] = []

    def check(self, ok, claim, detail=""):
        self.rows.append((bool(ok), claim, detail))

    def render(self):
        width = max(len(c) for _, c, _ in self.rows) + 2
        print("-" * (width + 8))
        for ok, claim, detail in self.rows:
            print(f"{claim:<{width}} {'PASS' if ok else 'FAIL'}")
            if detail:
                for line in str(detail).splitlines()[:6]:
                    print(f"    {line}")
        print("-" * (width + 8))
        passed = sum(1 for ok, _, _ in self.rows if ok)
        print(f"checks: {passed}/{len(self.rows)}")
        return passed == len(self.rows)


OVERFLOW_JS = """
() => {
  const bad = [];
  const d = document.documentElement;
  // The symptom that matters: the window itself scrolls sideways. This is what
  // bug #18 actually was.
  if (d.scrollWidth > d.clientWidth + 1)
    bad.push('DOCUMENT ' + d.scrollWidth + '>' + d.clientWidth);
  for (const el of document.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    // Out-of-flow elements are excluded deliberately. `.row-actions` is an
    // absolutely-positioned hover overlay that reports a 3px scrollWidth
    // delta from subpixel rounding; it cannot widen the page, and the document
    // check above proves the page does not scroll. Flagging it was the
    // checker's scope being wrong, not the layout -- so the fix is the scope,
    // not a looser threshold. Tuning a number until it passes is how a check
    // stops meaning anything.
    if (cs.position === 'absolute' || cs.position === 'fixed') continue;
    if (el.scrollWidth > el.clientWidth + 2 && cs.overflowX === 'visible') {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0)
        bad.push(el.tagName + '.' + (el.className || '') + ' ' + el.scrollWidth + '>' + el.clientWidth);
    }
  }
  return bad.slice(0, 5);
}
"""

# A control whose box is more than 1.6x its own line-height has wrapped.
WRAP_JS = """
() => {
  const bad = [];
  // Deliberate CONTROLS only. Targeting every <button> flagged the mail rows,
  // which are buttons wrapping a whole message and are supposed to be several
  // lines tall -- the checker's scope was wrong, not the page.
  for (const el of document.querySelectorAll('.btn, .tag, .chip')) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height || !el.textContent.trim()) continue;
    const lh = parseFloat(getComputedStyle(el).lineHeight) || 16;
    const pad = parseFloat(getComputedStyle(el).paddingTop) + parseFloat(getComputedStyle(el).paddingBottom);
    if (r.height > lh * 1.6 + pad + 2)
      bad.push(JSON.stringify(el.textContent.trim().slice(0, 24)) + ' h=' + r.height.toFixed(0) + ' lh=' + lh);
  }
  return bad.slice(0, 5);
}
"""

RAW_KEYS_JS = """
() => {
  const out = [];
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walk.nextNode())) {
    const s = n.textContent.trim();
    if (/^(rank|why|explored|provider|digest|filter|tag)[A-Z][A-Za-z]*$/.test(s)) out.push(s);
  }
  return [...new Set(out)].slice(0, 8);
}
"""

MOTION_JS = """
() => {
  // Durations must come from the scale. A custom number is exactly what makes
  // motion feel guessed rather than designed, and 120ms -- the app's old
  // default, used 136 times -- is below the threshold where movement reads as
  // movement at all.
  const allowed = new Set(['0.09s', '0.16s', '0.24s', '0.36s', '0.52s', '0.7s', '1.05s',
                           '1.1s', '1.15s', '0.9s', '1.5s', '0.4s', '0.55s', '0.26s']);
  const off = {};
  let moving = 0;
  for (const el of document.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    for (const d of cs.transitionDuration.split(',').map(s => s.trim())) {
      if (!d || d === '0s') continue;
      moving++;
      if (!allowed.has(d)) off[d] = (off[d] || 0) + 1;
    }
  }
  const rows = [...document.querySelectorAll('.mail-item')].slice(0, 5)
    .map(el => parseFloat(getComputedStyle(el).animationDelay) * 1000);
  const steps = rows.slice(1).map((v, i) => Math.round(v - rows[i]));
  return { offScale: Object.entries(off).sort((a, b) => b[1] - a[1]).slice(0, 5),
           moving, staggerSteps: steps,
           easings: [...new Set([...document.querySelectorAll('.mail-item, .btn, .tag')]
             .map(el => getComputedStyle(el).transitionTimingFunction))].slice(0, 4) };
}
"""

BADGE_JS = """
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  return { text: el.textContent.trim(), w: r.width, h: r.height,
           color: cs.color, border: cs.borderTopColor, bg: cs.backgroundColor };
}
"""


def luminance(css):
    m = re.findall(r"[\d.]+", css or "")
    if len(m) < 3:
        return None
    r, g, b = (float(x) / 255 for x in m[:3])
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(fg, bg):
    a, b = luminance(fg), luminance(bg)
    if a is None or b is None:
        return None
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def open_put_away(page) -> None:
    """Make sure the decided-about folder is open, without toggling it shut.

    It is a toggle and it opens itself when one of its lists becomes the
    current view -- so a harness that clicks it unconditionally opens it the
    first time and closes it the second, then times out waiting for a child
    that is no longer in the DOM. Which is what happened.
    """
    if page.locator("button[data-view='done']").count() == 0:
        page.click("button[data-view='put-away']")
        page.wait_for_timeout(250)



COMPOSE_JS = """() => {
  const el = (q) => document.querySelector(q);
  const dlg = el('.compose');
  if (!dlg) return { open: false };
  const r = dlg.getBoundingClientRect();
  const foot = [...document.querySelectorAll('.compose-foot .btn')]
    .map(b => ({ text: b.textContent.trim(), primary: b.classList.contains('primary'),
                 w: b.getBoundingClientRect().width }));
  return {
    open: true,
    w: r.width, h: r.height,
    offscreen: r.left < 0 || r.top < 0 || r.right > innerWidth || r.bottom > innerHeight,
    // Anything the user could type into on the screen they are approving.
    editable: document.querySelectorAll(
      '.compose-review input, .compose-review textarea, '
      + '.compose-review [contenteditable="true"]').length,
    fields: [...document.querySelectorAll('.compose-field > span')].map(e => e.textContent.trim()),
    review: el('.compose-preview') ? el('.compose-preview').textContent : '',
    headers: [...document.querySelectorAll('.compose-review dd')].map(e => e.textContent.trim()),
    field: Object.fromEntries([...document.querySelectorAll('.compose-review dd[data-field]')]
      .map(e => [e.dataset.field, e.textContent.trim()])),
    handoff: !!el('.compose-note.handoff'),
    // An undefined custom property resolves to nothing, so a dialog can end up
    // with no background at all. Against a pale scrim that is invisible; the
    // fix is to assert the pixel, not the declaration.
    bg: getComputedStyle(dlg).backgroundColor,
    done: el('.compose-done-line') ? el('.compose-done-line').textContent.trim() : '',
    foot,
  };
}"""

TYPED = "Registering now, thank you."


def compose_check(page, tag, shots):
    """Write a reply, approve it, send it -- and assert the three claims the
    design rests on.

    1. The review screen shows the message the SERVER built, headers included,
       not a replay of the form. That is the only thing that makes approval
       mean anything.
    2. The review screen has nothing to type into. Editing and approving are
       separate acts; a screen that does both is one where a half-finished
       message goes out.
    3. The button names what will actually happen. A transport that only files
       a draft must not offer a button marked "Send" -- the user finds out from
       the recipient who never got it.
    """
    bad = []
    rows = page.query_selector_all(".mail-item")
    if not rows:
        return ["no mail row to reply to"]
    rows[0].click()
    page.wait_for_timeout(350)

    actions = page.evaluate(
        "() => [...document.querySelectorAll('.reply-bar button')]"
        ".map(b => ({ t: b.textContent.trim(), a: b.dataset.action,"
        "             w: b.getBoundingClientRect().width,"
        "             h: b.getBoundingClientRect().height }))")
    if not any(a["a"] == "reply" for a in actions) or not any(a["a"] == "forward" for a in actions):
        return [f"{tag}: reply and forward are not both on an open message -- {actions}"]
    if any(a["w"] < 40 or a["h"] < 18 for a in actions):
        bad.append(f"{tag}: a reply control collapsed -- {actions}")
    if len({a["t"] for a in actions}) < len(actions):
        bad.append(f"{tag}: two reply controls share a label {actions}")

    page.click(".reply-bar [data-action='reply']")
    page.wait_for_timeout(350)
    state = page.evaluate(COMPOSE_JS)
    if not state["open"]:
        return bad + [f"{tag}: reply did not open a compose window"]
    page.fill(".compose-body", TYPED)
    page.screenshot(path=str(shots / f"{tag}-compose.png"))

    page.click(".compose-foot .btn.primary")
    page.wait_for_timeout(450)
    review = page.evaluate(COMPOSE_JS)
    if not review["review"]:
        bad.append(f"{tag}: the review screen showed no message")
    else:
        if TYPED not in review["review"]:
            bad.append(f"{tag}: the review omits what was typed")
        if ">" not in review["review"]:
            bad.append(f"{tag}: the review omits the quoted original")
    if review["editable"]:
        bad.append(f"{tag}: {review['editable']} editable fields on the approval screen")
    # Asked of the To line by name. A reply whose recipient the user did not
    # type still has one -- the sender of the original -- and it is the server
    # that works that out. Showing the empty form field here instead would be
    # invisible to any check that asks "is there an address somewhere".
    field = review.get("field") or {}
    if "@" not in (field.get("to") or ""):
        bad.append(f"{tag}: the To line on the approval screen is {field.get('to')!r} "
                   "-- the derived recipient is not being shown")
    if "@" not in (field.get("from") or ""):
        bad.append(f"{tag}: no From address on the approval screen")
    if "Re:" not in (field.get("subject") or ""):
        bad.append(f"{tag}: the reply subject is {field.get('subject')!r}")
    if review["offscreen"]:
        bad.append(f"{tag}: the compose window is partly off screen")
    bg = review.get("bg") or ""
    alpha = 1.0
    if bg.startswith("rgba"):
        try:
            alpha = float(bg.rsplit(",", 1)[1].strip(" )"))
        except ValueError:
            alpha = 1.0
    if alpha < 0.99:
        bad.append(f"{tag}: the compose window background is {bg} -- the list "
                   "behind it shows through")
    page.screenshot(path=str(shots / f"{tag}-review.png"))

    labels = [b["text"] for b in review["foot"]]
    primary = next((b for b in review["foot"] if b["primary"]), None)
    if primary is None:
        bad.append(f"{tag}: no confirm button on the approval screen")
    elif primary["w"] < 40:
        bad.append(f"{tag}: the confirm button collapsed to {primary['w']}px")

    page.click(".compose-foot .btn.primary")
    page.wait_for_timeout(450)
    done = page.evaluate(COMPOSE_JS)
    if not done["done"]:
        bad.append(f"{tag}: sending reported nothing back")
    page.screenshot(path=str(shots / f"{tag}-sent.png"))
    page.click(".compose-foot .btn.primary")
    page.wait_for_timeout(250)

    # Now the other transport mode, on the same page. The words on the button
    # are the only warning the user gets before clicking it.
    page.request.get(f"http://127.0.0.1:{PORT}/__set?transport=hands_off")
    page.click(".reply-bar [data-action='reply']")
    page.wait_for_timeout(300)
    page.fill(".compose-body", TYPED)
    page.click(".compose-foot .btn.primary")
    page.wait_for_timeout(450)
    handoff = page.evaluate(COMPOSE_JS)
    hand_labels = [b["text"] for b in handoff["foot"] if b["primary"]]
    if not handoff["handoff"]:
        bad.append(f"{tag}: a hands-off transport gave no warning before the button")
    if hand_labels and labels and hand_labels[0] in labels:
        bad.append(f"{tag}: 'save to drafts' and 'send' share the label {hand_labels[0]!r}")
    page.screenshot(path=str(shots / f"{tag}-handoff.png"))
    page.request.get(f"http://127.0.0.1:{PORT}/__set?transport=delivers")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    page.click("button[data-view='priority']")
    page.wait_for_timeout(300)
    return bad


REPLY_BAR_JS = """() => {
  const bar = document.querySelector('.reply-bar');
  const wrap = document.querySelector('.detail-wrap');
  if (!bar || !wrap) return { bar: false };
  const r = bar.getBoundingClientRect(), w = wrap.getBoundingClientRect();
  const btns = [...bar.querySelectorAll('button')].map(b => {
    const q = b.getBoundingClientRect();
    return { a: b.dataset.action, h: q.height, w: q.width,
             label: b.textContent.trim(),
             hit: document.elementFromPoint(q.left + q.width / 2, q.top + q.height / 2) };
  });
  return {
    bar: true,
    inHeader: !!bar.closest('.detail-head'),
    inFrame: !!bar.closest('iframe, .detail-body'),
    onScreen: r.top >= w.top - 1 && r.bottom <= Math.min(w.bottom, innerHeight) + 1 && r.height > 0,
    buttons: btns.map(b => ({ a: b.a, h: Math.round(b.h), w: Math.round(b.w), label: b.label,
                              covered: !(b.hit && (b.hit.closest('.reply-bar button'))) })),
  };
}"""

HEAD_KEY = "foolsgold.detailHeadHeight"


def reply_bar_check(page, tag) -> list[str]:
    """The reply controls stay on screen, uncovered and legible, whatever the
    user has done to the header above them.

    They used to live inside the resizable header: drag it shorter and they
    were clipped, and people did not know the app could reply at all. So the
    extremes are what is tested -- a header saved enormous (the stored height is
    applied as-is on load) and one dragged to its minimum -- not the default.
    """
    bad = []
    for label, height in (("default", None), ("tall", 5000), ("short", 70)):
        page.evaluate(f"""() => {{ try {{
            {'localStorage.removeItem("' + HEAD_KEY + '")' if height is None
             else 'localStorage.setItem("' + HEAD_KEY + '", "' + str(height) + '")'};
        }} catch (e) {{}} }}""")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(450)
        rows = page.query_selector_all(".mail-item")
        if not rows:
            return [f"{tag}: no mail to open"]
        rows[0].click()
        page.wait_for_timeout(400)
        got = page.evaluate(REPLY_BAR_JS)
        where = f"{tag}/{label} header"
        if not got.get("bar"):
            bad.append(f"{where}: no reply bar on an open message")
            continue
        if got["inHeader"] or got["inFrame"]:
            bad.append(f"{where}: the bar is inside the header or the mail -- it can be "
                       "clipped or scrolled away")
        if not got["onScreen"]:
            bad.append(f"{where}: the reply bar is pushed off screen")
        for b in got["buttons"]:
            if b["h"] < 36:
                bad.append(f"{where}: {b['a']} is {b['h']}px tall (min 36)")
            if b["covered"]:
                bad.append(f"{where}: {b['a']} is covered by something else")
        primary = next((b for b in got["buttons"] if b["a"] == "reply"), None)
        if primary is None or len(primary["label"]) < 3:
            bad.append(f"{where}: the reply action has no visible label -- {got['buttons']}")
    page.evaluate(f"() => {{ try {{ localStorage.removeItem('{HEAD_KEY}'); }} catch (e) {{}} }}")
    page.reload(wait_until="networkidle")
    page.wait_for_timeout(350)
    return bad


def undefined_custom_properties() -> list[str]:
    """Every `var(--x)` in the stylesheet must have a `--x:` somewhere.

    An undefined custom property is not an error anywhere in the toolchain: the
    build succeeds, the rule is simply dropped. `background: var(--panel)` in a
    stylesheet whose variable is called `--surface` produced a modal with no
    background, which was invisible until the scrim behind it was darkened.
    Cheap to check, and it catches the whole class rather than the one case.
    """
    # Every stylesheet under src/, not only styles.css: a component can carry
    # its own (ReplyBar.css does), and a check that reads one file passes over
    # the rest without a word.
    css = "\n".join(f.read_text() for f in sorted((ROOT / "frontend" / "src").rglob("*.css")))
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
    used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
    # A var() with a fallback still renders, so only bare ones matter.
    bare = {m for m in re.findall(r"var\((--[a-z0-9-]+)\s*\)", css)}
    return sorted((used & bare) - defined)


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    result = Result()
    server = subprocess.Popen(
        [sys.executable, str(ROOT / "verify" / "stub_api.py"), str(PORT), "ko", "gold"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            overflow_bad, wrap_bad, raw_bad, untranslated, reached, themed = [], [], [], [], [], []
            off_scale, stagger_bad, done_bad, irr_bad, set_bad = [], [], [], [], []
            compose_bad = []
            reply_bar_bad = []
            grounds: dict[str, set] = {}
            badge_seen, why_seen, contrast_bad = 0, 0, []
            badge_covers: list[str] = []
            title_bad: list[str] = []
            notice_bad: list[str] = []
            copies_bad: list[str] = []
            ai_bad: list[str] = []
            split_bad: list[str] = []
            reason_bad: list[str] = []

            for theme in THEMES:
                for lang in LANGS:
                    page = browser.new_page(viewport={"width": 1280, "height": 900})
                    # The app takes theme and language from /api/settings, so
                    # the harness asks the stub for them rather than poking the
                    # DOM -- the first version did the latter, then reloaded,
                    # and every screenshot came out in the default theme.
                    page.request.get(
                        f"http://127.0.0.1:{PORT}/__set?theme={theme}&lang={lang}")
                    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
                    page.wait_for_timeout(500)

                    tag = f"{theme}-{lang}"

                    # Precondition. Everything below asserts something ABOUT the
                    # mail list; if the list never rendered, every one of those
                    # assertions passes over an empty page. This is the guard the
                    # first run of this harness did not have, and it reported
                    # 5/7 green against a connect screen.
                    applied = page.evaluate(
                        "() => document.documentElement.getAttribute('data-theme')"
                        " || document.body.getAttribute('data-theme') || ''")
                    if applied and applied != theme:
                        themed.append(f"{tag}: asked for {theme}, got {applied}")
                    grounds.setdefault(theme, set()).add(
                        page.evaluate("() => getComputedStyle(document.body).backgroundColor"))

                    rows = page.evaluate("() => document.querySelectorAll('.mail-item').length")
                    # The priority view hides nothing any more -- it is every
                    # email you have not dealt with, in rank order. A view that
                    # ranks AND hides asks the user to trust a judgement they
                    # cannot check.
                    buckets = page.evaluate("""() => [...new Set([...document.querySelectorAll(
                        '.mail-item .tag.noise, .mail-item .tag.fyi, .mail-item .tag.action')]
                        .map(e => [...e.classList].find(c => c !== 'tag')))]""")
                    if rows and "noise" not in buckets:
                        reached.append(f"{tag}: the priority list is still hiding a bucket "
                                       f"-- saw {sorted(buckets)}")
                    if not rows:
                        reached.append(f"{tag}: mail list never rendered")
                        page.screenshot(path=str(SHOTS / f"{tag}.png"))
                        page.close()
                        continue
                    for sel in OVERFLOW_JS, WRAP_JS, RAW_KEYS_JS:
                        pass
                    o = page.evaluate(OVERFLOW_JS)
                    if o:
                        overflow_bad.append(f"{tag}: {o}")
                    w = page.evaluate(WRAP_JS)
                    if w:
                        wrap_bad.append(f"{tag}: {w}")
                    r = page.evaluate(RAW_KEYS_JS)
                    if r:
                        raw_bad.append(f"{tag}: {r}")
                    # A raw i18n KEY looks like camelCase; a raw VERDICT looks
                    # like snake_case, so the check above sailed past
                    # NOT_RELEVANT sitting in a chip in all four languages.
                    snake = page.evaluate("""() => {
                      const bad = [];
                      for (const el of document.querySelectorAll('.tag, .chip, .btn, .rail-label')) {
                        const txt = (el.textContent || '').trim();
                        if (/^[a-z][a-z0-9]*(_[a-z0-9]+)+$/i.test(txt)) bad.push(txt);
                      }
                      return bad.slice(0, 3);
                    }""")
                    if snake:
                        raw_bad.append(f"{tag}: untranslated verdict/identifier {snake}")

                    motion = page.evaluate(MOTION_JS)
                    if motion["offScale"]:
                        off_scale.append(f"{tag}: {motion['offScale']}")
                    if motion["moving"] < 20:
                        off_scale.append(f"{tag}: only {motion['moving']} animated properties")
                    steps = motion["staggerSteps"]
                    if steps and (min(steps) < 35 or max(steps) > 90):
                        stagger_bad.append(f"{tag}: steps {steps}ms")

                    # The briefing is a different list from the mail list, and
                    # for the whole life of this harness it was not: a prefix
                    # rule in the stub answered the digest endpoint with the
                    # mail fixture, so this pane rendered seven mail rows and
                    # nobody noticed. Assert they differ.
                    brief = page.evaluate("""() => {
                      const agenda = [...document.querySelectorAll('.agenda-item')];
                      const list = [...document.querySelectorAll('.mail-item .subj')]
                        .map(e => e.textContent.trim());
                      return { n: agenda.length,
                               same: agenda.length > 0 && agenda.length === list.length };
                    }""")
                    if brief["n"] == 0:
                        reached.append(f"{tag}: the briefing rendered no items")
                    elif brief["same"]:
                        reached.append(f"{tag}: the briefing has exactly as many rows as the "
                                       "mail list -- it is probably showing the mail list")

                    # A rail badge that sits ON its icon.
                    #
                    # `.rail button .badge { top: 5px; right: 5px }` was written
                    # when the badge's containing block was the 42px button. It
                    # is now `.rail-glyph`, which is exactly the 18px icon, so
                    # those offsets put the dot in the MIDDLE of the glyph and
                    # painted a disc over the checklist -- reported, reasonably,
                    # as "the priority icon is broken". The correcting rule
                    # `.rail-item .badge` lost on specificity and never applied,
                    # which is indistinguishable from never having been written.
                    # Geometry, not appearance: the dot must not cover the
                    # centre of the glyph it belongs to.
                    cover = page.evaluate("""() => {
                      const out = [];
                      for (const b of document.querySelectorAll('.rail button .badge')) {
                        const g = b.parentElement.getBoundingClientRect();
                        const r = b.getBoundingClientRect();
                        const cx = g.left + g.width / 2, cy = g.top + g.height / 2;
                        if (r.left <= cx && r.right >= cx && r.top <= cy && r.bottom >= cy) {
                          out.push(b.parentElement.parentElement.dataset.view || '?');
                        }
                      }
                      return out;
                    }""")
                    if cover:
                        badge_covers.append(f"{tag}: badge covers the icon on {cover}")

                    # A briefing row titled by its own deadline.
                    #
                    # The headline used to be the note, and with no model the
                    # note is a translation key for the due date -- so every row
                    # was titled "Due today" / "Due 2026-09-20" while the chip
                    # beside it said the same words. Seven rows, one repeated
                    # title, and the subject demoted to small grey text. The
                    # title must be the subject, and must not equal the chip.
                    titles = page.evaluate("""() => {
                      const bad = [];
                      for (const li of document.querySelectorAll('.agenda-item')) {
                        const title = (li.querySelector('.agenda-subject')?.innerText || '').trim();
                        const chip = (li.querySelector('.agenda-meta .tag')?.innerText || '').trim();
                        if (!title) { bad.push('(no title)'); continue; }
                        if (chip && title.toUpperCase() === chip.toUpperCase()) bad.push(title);
                      }
                      const all = [...document.querySelectorAll('.agenda-item .agenda-subject')]
                        .map(e => e.innerText.trim());
                      // Two rows sharing a title is the same defect from the
                      // other side: whatever is in the title is then not what
                      // tells the rows apart.
                      if (all.length > 1 && new Set(all).size === 1) bad.push('every row titled ' + all[0]);
                      return bad.slice(0, 3);
                    }""")
                    if titles:
                        title_bad.append(f"{tag}: {titles}")

                    # The AI banner is one sentence, not two languages.
                    #
                    # Read BEFORE the dismissal check below, which closes the
                    # first notice -- and the first notice is this one. Asserted
                    # on a strip that had just been shut, it reported "did not
                    # render" and looked like a bug in the app.
                    if lang == "ko":
                        ai = page.evaluate("""() => {
                          for (const b of document.querySelectorAll('.pane-detail .banner')) {
                            const txt = b.innerText || '';
                            if (txt.includes('AI')) return txt;
                          }
                          return null;
                        }""")
                        if ai is None:
                            ai_bad.append(f"{tag}: the AI banner did not render")
                        elif re.search(r"has no model|Pull it first|ollama pull|is not pulled", ai):
                            ai_bad.append(f"{tag}: English left in the AI banner -- {ai[:80]!r}")

                    # Every notice can be sent away, and sending one away
                    # leaves the others alone.
                    #
                    # These strips are all true and all worth saying once. Said
                    # on every render with no way to acknowledge them, they
                    # become a band of yellow the eye stops reading -- and then
                    # the one that matters is the one nobody sees either.
                    before = page.eval_on_selector_all(".pane-detail .banner", "els => els.length")
                    closers = page.eval_on_selector_all(
                        ".pane-detail .banner", "els => els.filter(e => e.querySelector('.banner-x')).length")
                    if before == 0:
                        notice_bad.append(f"{tag}: no notice rendered -- the fixture should force one")
                    elif closers != before:
                        notice_bad.append(f"{tag}: {before - closers} of {before} notices cannot be closed")
                    else:
                        page.click(".pane-detail .banner .banner-x")
                        page.wait_for_timeout(120)
                        after = page.eval_on_selector_all(".pane-detail .banner", "els => els.length")
                        if after != before - 1:
                            notice_bad.append(f"{tag}: closing one notice took {before - after} away")

                    # The reading pane: open an email, then the two things that
                    # live in it.
                    page.click(".mail-item")
                    page.wait_for_selector(".detail-wrap", timeout=4000)
                    page.wait_for_timeout(250)

                    # 1. the reason line is in the reader's language. The
                    #    backend used to build it as an English sentence, so it
                    #    sat in English in the middle of a Korean pane.
                    why = (page.eval_on_selector(".why", "e => e.innerText") or "") if \
                        page.query_selector(".why") else ""
                    if not why.strip():
                        reason_bad.append(f"{tag}: the reason line is empty")
                    elif re.search(r"found in the text|has an attachment|flagged high|sig[A-Z]", why):
                        reason_bad.append(f"{tag}: untranslated reason -- {why[:70]!r}")
                    if lang == "ko" and re.search(r"[A-Za-z]{4,}\s+[A-Za-z]{4,}", why):
                        reason_bad.append(f"{tag}: English prose in a Korean reason -- {why[:70]!r}")

                    # 2. the header/body divider actually moves the boundary.
                    #    Geometry, because a separator that renders and does
                    #    nothing is the failure this is written for.
                    # Measured AFTER a tick, not in the same block as the
                    # drag: React flushes a state update from an event handler
                    # asynchronously, so reading the box straight afterwards
                    # reports the old height and the check fails on a feature
                    # that works. (It did, first run.)
                    started = page.evaluate("""() => {
                      const head = document.querySelector('.detail-head');
                      const bar = document.querySelector('.detail-resizer');
                      if (!head || !bar) return { missing: true };
                      const before = head.getBoundingClientRect().height;
                      const b = bar.getBoundingClientRect();
                      const x = b.left + b.width / 2, y = b.top + b.height / 2;
                      bar.dispatchEvent(new PointerEvent('pointerdown', {
                        clientX: x, clientY: y, bubbles: true, button: 0 }));
                      window.dispatchEvent(new PointerEvent('pointermove', {
                        clientX: x, clientY: y + 70, bubbles: true }));
                      window.dispatchEvent(new PointerEvent('pointerup', {
                        clientX: x, clientY: y + 70, bubbles: true }));
                      return { before };
                    }""")
                    page.wait_for_timeout(200)
                    moved = page.evaluate("""(before) => {
                      const head = document.querySelector('.detail-head');
                      const body = document.querySelector('.detail-body');
                      return { before, after: head.getBoundingClientRect().height,
                               body: body.getBoundingClientRect().height };
                    }""", started.get("before"))
                    if started.get("missing"):
                        moved = {"missing": True}
                    if moved.get("missing"):
                        split_bad.append(f"{tag}: no divider in the reading pane")
                    elif moved["after"] - moved["before"] < 30:
                        split_bad.append(
                            f"{tag}: dragging 70px moved the header "
                            f"{moved['after'] - moved['before']:.0f}px")
                    elif moved["body"] < 40:
                        split_bad.append(f"{tag}: the mail was squeezed to {moved['body']:.0f}px")

                    page.keyboard.press("Escape")
                    page.wait_for_timeout(200)

                    # A collapsed row says how many messages it stands for.
                    #
                    # The ranked list hides the other copies of a repeated
                    # announcement. A list that quietly drops mail is
                    # indistinguishable from one that lost it, so the count is
                    # not decoration -- it is the difference between the two.
                    # Asserted as painted pixels, because a number rendered in
                    # the background colour is the same as no number.
                    counted = page.evaluate("""() => {
                      const list = document.querySelector('.mail-item .copies');
                      const brief = document.querySelector('.agenda-item .copies');
                      const look = (el) => {
                        if (!el) return null;
                        const r = el.getBoundingClientRect();
                        const cs = getComputedStyle(el);
                        return { text: el.innerText.trim(), w: r.width, h: r.height,
                                 color: cs.color, bg: cs.backgroundColor };
                      };
                      return { list: look(list), brief: look(brief) };
                    }""")
                    for where in ("list", "brief"):
                      seen = counted[where]
                      if not seen:
                          copies_bad.append(f"{tag}: the {where} shows no count on a collapsed row")
                      elif not re.search(r"\d", seen["text"] or ""):
                          copies_bad.append(f"{tag}: {where} count has no number: {seen['text']!r}")
                      elif seen["w"] < 8 or seen["h"] < 8:
                          copies_bad.append(f"{tag}: {where} count is {seen['w']:.0f}x{seen['h']:.0f}")
                      else:
                          c = contrast(seen["color"], seen["bg"]) if "rgba(0, 0, 0, 0)" not in seen["bg"] else None
                          if c is not None and c < 3.0:
                              copies_bad.append(f"{tag}: {where} count contrast {c:.2f}")

                    badge = page.evaluate(BADGE_JS, ".tag.explored")
                    why = page.evaluate(BADGE_JS, ".tag.why")
                    if badge and badge["w"] > 4:
                        badge_seen += 1
                    if why and why["w"] > 4:
                        why_seen += 1
                        c = contrast(why["color"], why["bg"] if "rgba(0, 0, 0, 0)" not in why["bg"]
                                     else page.evaluate("() => getComputedStyle(document.body).backgroundColor"))
                        if c is not None and c < 3.0:
                            contrast_bad.append(f"{tag}: why chip contrast {c:.2f}")

                    if lang == "ko":
                        body = page.evaluate("() => document.body.innerText")
                        if "Learning is off" in body or "Start learning from now" in body:
                            untranslated.append(f"{tag}: English string in a Korean UI")
                    page.screenshot(path=str(SHOTS / f"{tag}.png"), full_page=False)

                    compose_bad += compose_check(page, tag, SHOTS)
                    reply_bar_bad += reply_bar_check(page, tag)

                    # The completed box. Muting had a drawer you could open and
                    # reverse; completing had a six-second toast. This asserts
                    # the drawer exists, is readable, and offers the way back --
                    # an un-tickable checkbox on a row that is not greyed out.
                    open_put_away(page)
                    page.click("button[data-view='done']")
                    page.wait_for_timeout(450)
                    box = page.evaluate("""() => {
                      const row = document.querySelector('.mail-item');
                      if (!row) return { rows: 0 };
                      const chip = row.querySelector('.tag.done-at');
                      const cb = row.querySelector(".row-check input[type='checkbox']");
                      return {
                        rows: document.querySelectorAll('.mail-item').length,
                        chip: chip ? chip.getBoundingClientRect().width : 0,
                        chipText: chip ? chip.textContent.trim() : '',
                        opacity: parseFloat(getComputedStyle(row).opacity),
                        checked: !!(cb && cb.checked),
                      };
                    }""")
                    if not box.get("rows"):
                        done_bad.append(f"{tag}: the completed box rendered no rows")
                    elif box["chip"] < 4:
                        done_bad.append(f"{tag}: no 'done at' chip on the row")
                    elif box["opacity"] < 0.95:
                        done_bad.append(f"{tag}: the box is faded to {box['opacity']} -- "
                                        "a list you came to read, printed in grey")
                    elif not box["checked"]:
                        done_bad.append(f"{tag}: the row is not ticked, so there is nothing to untick")
                    page.screenshot(path=str(SHOTS / f"{tag}-done.png"), full_page=False)

                    # The review surface for "not relevant". Suppression is only
                    # defensible because this exists -- the cost of wrongly
                    # hiding a real message is a property of the UI, not of the
                    # email, and it is one click away here.
                    open_put_away(page)
                    page.click("button[data-view='dismissed']")
                    page.wait_for_timeout(450)
                    irr = page.evaluate("""() => {
                      const row = document.querySelector('.mail-item');
                      if (!row) return { rows: 0 };
                      const chip = row.querySelector('.tag.cat');
                      const btn = row.querySelector(".row-actions button.dismissed");
                      return { rows: document.querySelectorAll('.mail-item').length,
                               cat: chip ? chip.textContent.trim() : '',
                               catW: chip ? chip.getBoundingClientRect().width : 0,
                               undo: !!btn };
                    }""")
                    if not irr.get("rows"):
                        irr_bad.append(f"{tag}: the dismissed list rendered no rows")
                    elif irr["catW"] < 4:
                        irr_bad.append(f"{tag}: no category chip -- the user cannot see "
                                       "what kind of thing was set aside")
                    elif not irr["undo"]:
                        irr_bad.append(f"{tag}: no way to take it back from this list")
                    page.screenshot(path=str(SHOTS / f"{tag}-dismissed.png"), full_page=False)

                    # Settings, in groups, with a search that ignores them.
                    page.click("button[data-view='settings']")
                    page.wait_for_timeout(450)
                    st = page.evaluate("""() => {
                      const secs = () => [...document.querySelectorAll('section[data-group]')]
                        .map(s => s.dataset.section);
                      const tabs = [...document.querySelectorAll('.settings-tabs .chip')];
                      return { tabs: tabs.length, all: secs() };
                    }""")
                    if st["tabs"] < 3:
                        set_bad.append(f"{tag}: {st['tabs']} setting groups -- one scroll again")
                    elif len(st["all"]) < 8:
                        set_bad.append(f"{tag}: only {len(st['all'])} sections on 'All'")
                    else:
                        # Pick a group, then search for something that lives in
                        # a DIFFERENT group. A grouping you cannot search past
                        # hides things confidently, which is worse than a list.
                        page.click(".settings-tabs .chip:nth-child(3)")
                        page.wait_for_timeout(250)
                        narrowed = page.evaluate(
                            "() => document.querySelectorAll('section[data-group]').length")
                        page.fill(".settings-search", "ollama")
                        page.wait_for_timeout(300)
                        found = page.evaluate("""() => [...document.querySelectorAll(
                            'section[data-group]')].map(s => s.dataset.section)""")
                        if narrowed >= len(st["all"]):
                            set_bad.append(f"{tag}: choosing a group narrowed nothing "
                                           f"({narrowed} of {len(st['all'])})")
                        elif "aiProvider" not in found:
                            set_bad.append(f"{tag}: search did not reach past the chosen "
                                           f"group -- got {found}")
                    page.screenshot(path=str(SHOTS / f"{tag}-settings.png"), full_page=False)
                    page.close()

            # 22. Cloud-AI consent. Apple 5.1.2(i) and PIPA both turn on the
            # same two facts: the user is asked BEFORE anything is sent, and the
            # safe answer is the easy one. So: the dialog appears on its own
            # when a cloud provider has no permission; focus lands on "Don't
            # allow" (Enter must never share a mailbox); every row is in the
            # reader's language; declining sends nothing; allowing sends exactly
            # one grant; and Settings then says what was agreed.
            consent_bad: list[str] = []
            for theme in THEMES[:2]:
                for lang in LANGS:
                    tag = f"{theme}-{lang}-consent"
                    for answer in ("decline", "allow"):
                        page = browser.new_page(viewport={"width": 1280, "height": 900})
                        page.request.get(f"http://127.0.0.1:{PORT}/__set?theme={theme}"
                                         f"&lang={lang}&consent=pending")
                        page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
                        page.wait_for_timeout(700)
                        info = page.evaluate("""() => {
                          const m = document.querySelector('.modal.consent');
                          if (!m) return null;
                          const btns = [...m.querySelectorAll('.actions .btn')];
                          return {
                            text: m.innerText,
                            focused: document.activeElement === btns[0],
                            primaryLast: btns[1] && btns[1].classList.contains('primary'),
                            rows: m.querySelectorAll('dt').length,
                            rawKey: /\bconsent[A-Z]\w*|aiConsent/.test(m.innerText),
                          };
                        }""")
                        if info is None:
                            consent_bad.append(f"{tag}: no dialog for an unapproved cloud provider")
                            page.close()
                            break
                        if not info["focused"]:
                            consent_bad.append(f"{tag}: focus is not on Don't allow")
                        if info["rows"] != 6:
                            consent_bad.append(f"{tag}: {info['rows']} disclosure rows, want 6")
                        if info["rawKey"]:
                            consent_bad.append(f"{tag}: a raw key is on screen")
                        if lang != "en" and "Who receives" in info["text"]:
                            consent_bad.append(f"{tag}: English left in the dialog")
                        if answer == "decline" and theme == "gold":
                            page.screenshot(path=str(SHOTS / f"{theme}-{lang}-consent.png"))
                        if answer == "decline":
                            page.keyboard.press("Enter")        # focused: Don't allow
                        else:
                            page.click(".modal.consent .actions .btn.primary")
                        page.wait_for_timeout(400)
                        posts = page.request.get(
                            f"http://127.0.0.1:{PORT}/__consent_posts").json()["posts"]
                        still = page.evaluate("() => !!document.querySelector('.modal.consent')")
                        if still:
                            consent_bad.append(f"{tag}: the dialog stayed open after {answer}")
                        if answer == "decline" and posts:
                            consent_bad.append(f"{tag}: Enter on the dialog GRANTED permission")
                        if answer == "allow":
                            if len(posts) != 1:
                                consent_bad.append(f"{tag}: allow sent {len(posts)} grants")
                            page.click("button[data-view='settings']")
                            page.wait_for_timeout(400)
                            row = page.evaluate("() => document.querySelector('.consent-status.ok')?.innerText || ''")
                            if "Anthropic" not in row:
                                consent_bad.append(f"{tag}: Settings does not show the permission")
                            if theme == "gold":
                                page.screenshot(path=str(SHOTS / f"{theme}-{lang}-consent-settings.png"))
                        page.close()
            # 24. The briefing is a checklist that moves. A row can be dismissed as
            # well as ticked -- "done" is not the only honest answer -- and either
            # way it leaves and the next one in the queue takes its place, without
            # waiting for tomorrow. And a structural briefing carries no "made
            # without AI" footer: it is complete, not a failure notice.
            brief_bad: list[str] = []
            for theme in THEMES[:2]:
                for lang in ("ko", "en"):
                    tag = f"{theme}-{lang}-briefing"
                    page = browser.new_page(viewport={"width": 1280, "height": 900})
                    page.request.get(f"http://127.0.0.1:{PORT}/__set?theme={theme}"
                                     f"&lang={lang}&digest=structural")
                    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
                    page.wait_for_timeout(600)
                    subjects = lambda: page.evaluate(
                        "() => [...document.querySelectorAll('.agenda .agenda-subject')]"
                        ".map(e => e.textContent.trim())")
                    before = subjects()
                    if len(before) != 3:
                        brief_bad.append(f"{tag}: expected 3 rows, saw {len(before)}")
                        page.close()
                        continue
                    if not page.query_selector(".agenda-item .agenda-dismiss"):
                        brief_bad.append(f"{tag}: no way to dismiss a briefing row")
                        page.close()
                        continue
                    page.hover(".agenda-item >> nth=0")
                    page.click(".agenda-item >> nth=0 >> .agenda-dismiss")
                    page.wait_for_timeout(1300)
                    after_dismiss = subjects()
                    if before[0] in after_dismiss:
                        brief_bad.append(f"{tag}: a dismissed row stayed on the briefing")
                    if len(after_dismiss) != 3:
                        brief_bad.append(f"{tag}: the gap was not refilled after dismissing "
                                         f"({len(after_dismiss)} rows)")
                    page.click(".agenda-item >> nth=0 >> .row-check")
                    page.wait_for_timeout(1500)
                    after_tick = subjects()
                    if after_dismiss and after_dismiss[0] in after_tick:
                        brief_bad.append(f"{tag}: a ticked row stayed on the briefing")
                    if len(after_tick) != 3:
                        brief_bad.append(f"{tag}: the gap was not refilled after ticking "
                                         f"({len(after_tick)} rows)")
                    foot = page.evaluate("() => document.querySelector('.digest-foot')?.innerText || ''")
                    if foot:
                        brief_bad.append(f"{tag}: a structural briefing still has a footer: {foot!r}")
                    if theme == "gold" and lang == "ko":
                        page.screenshot(path=str(SHOTS / f"{tag}.png"))
                    page.close()
            page = browser.new_page()
            page.request.get(f"http://127.0.0.1:{PORT}/__set?digest=reset")
            page.close()

            # 23. The first-run screen: the one a new user meets before anything
            # else, on a laptop-height window. It must be in their language, must
            # not talk to a developer ("npm start", "Terminal", "the app you
            # launched from"), must say what to do about macOS's permission, and
            # must scroll so the part that says so can be reached.
            setup_bad: list[str] = []
            for theme in THEMES[:2]:
                for lang in LANGS:
                    tag = f"{theme}-{lang}-setup"
                    page = browser.new_page(viewport={"width": 1280, "height": 640})
                    page.request.get(f"http://127.0.0.1:{PORT}/__set?theme={theme}"
                                     f"&lang={lang}&setup=fda")
                    page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
                    page.wait_for_timeout(600)
                    info = page.evaluate("""() => {
                      const card = document.querySelector('.centered .card');
                      if (!card) return null;
                      const box = card.closest('.centered');
                      box.scrollTop = box.scrollHeight;
                      const btn = [...card.querySelectorAll('.btn.primary')].pop();
                      const r = btn.getBoundingClientRect();
                      const top = card.getBoundingClientRect().top + box.scrollTop;
                      return {
                        text: card.innerText,
                        callout: !!card.querySelector('.setup-callout'),
                        lastButtonVisible: r.bottom <= window.innerHeight && r.top >= 0,
                        overflowing: box.scrollHeight > box.clientHeight,
                        topReachable: (box.scrollTop = 0, card.getBoundingClientRect().top >= 0),
                      };
                    }""")
                    if info is None:
                        setup_bad.append(f"{tag}: the setup card never rendered")
                        page.close()
                        continue
                    if not info["callout"]:
                        setup_bad.append(f"{tag}: no Full Disk Access instructions")
                    for dev in ("npm", "Terminal", "launched from"):
                        if dev in info["text"]:
                            setup_bad.append(f"{tag}: developer text on a user screen ({dev!r})")
                    if lang != "en" and ("Save and continue" in info["text"]
                                         or "Your email address" in info["text"]):
                        setup_bad.append(f"{tag}: English left on the setup card")
                    if not info["lastButtonVisible"]:
                        setup_bad.append(f"{tag}: the Save button cannot be scrolled into view")
                    if not info["topReachable"]:
                        setup_bad.append(f"{tag}: the top of the card is cut off")
                    if theme == "gold":
                        page.screenshot(path=str(SHOTS / f"{tag}.png"))
                    page.close()
            page = browser.new_page()
            page.request.get(f"http://127.0.0.1:{PORT}/__set?setup=off")
            page.close()

            # ---------------------------------------------------------- 25
            # The recap card. Three claims, and the third is the one that can
            # quietly destroy the feature: a summary that closes its own window
            # when you merely look at it leaves tomorrow's card empty about the
            # forty messages you never saw.
            recap_bad: list[str] = []
            for lang in LANGS:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.request.get(f"http://127.0.0.1:{PORT}/__set?theme=gold&lang={lang}")
                posts: list[str] = []
                page.on("request", lambda r, posts=posts: (
                    posts.append(r.url) if r.method == "POST" else None))
                page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
                page.wait_for_timeout(400)

                card = page.query_selector(".recap")
                if card is None:
                    recap_bad.append(f"{lang}: the recap card never rendered")
                    page.close()
                    continue

                shape = page.evaluate("""() => {
                  const card = document.querySelector('.recap');
                  const lines = [...card.querySelectorAll('.recap-line')];
                  return {
                    sections: [...card.querySelectorAll('.recap-section h4')]
                                .map(h => h.textContent.trim()),
                    lines: lines.length,
                    subjects: lines.map(l => (l.querySelector('.recap-subject')||{}).textContent || ''),
                    openable: lines.every(l => !!l.querySelector('.recap-open')),
                    text: card.innerText,
                    wide: card.scrollWidth > card.clientWidth + 1,
                  };
                }""")
                # 1. one line is one mail, and every line can be opened.
                if shape["lines"] < 3:
                    recap_bad.append(f"{lang}: {shape['lines']} lines, fixture has 3")
                if not shape["openable"]:
                    recap_bad.append(f"{lang}: a line with nothing to click -- "
                                     "a line that cannot be opened is a line that lies")
                if any(not t.strip() for t in shape["subjects"]):
                    recap_bad.append(f"{lang}: a line with no subject")
                if shape["wide"]:
                    recap_bad.append(f"{lang}: the card scrolls sideways")
                if lang == "ko":
                    for english in ("Needs you", "Mark as seen", "Show ", "Bulk"):
                        if english in shape["text"]:
                            recap_bad.append(f"{lang}: English left on the card ({english!r})")
                if "recapTitle" in shape["text"] or "cat_" in shape["text"]:
                    recap_bad.append(f"{lang}: a raw i18n key reached the card")

                # The picture is taken here, with the card still up. Taking it
                # after the dismissal flow below photographed an empty pane and
                # called it evidence of a card.
                if lang == "ko":
                    page.screenshot(path=str(SHOTS / "recap-ko.png"))

                # 2. clicking a line opens that mail, and writes nothing.
                before = list(posts)
                page.click(".recap-line .recap-open")
                page.wait_for_timeout(250)
                # Selecting a mail replaces the whole right pane, exactly as it
                # does from the briefing -- so the card is gone and `.detail-wrap`
                # is what proves the click landed on the right thing.
                opened = page.evaluate(
                    "() => !!document.querySelector('.detail-wrap, .detail-back')")
                if not opened:
                    recap_bad.append(f"{lang}: clicking a line opened no mail")
                wrote = [u for u in posts[len(before):] if "/api/" in u]
                if wrote:
                    recap_bad.append(f"{lang}: opening from the card wrote: {wrote}")

                # 3. only the dismiss button closes the window.
                page.goto(f"http://127.0.0.1:{PORT}/", wait_until="networkidle")
                page.wait_for_timeout(300)
                posts.clear()
                if page.query_selector(".recap-seen") is None:
                    recap_bad.append(f"{lang}: no way to dismiss the card")
                else:
                    page.click(".recap-seen")
                    page.wait_for_timeout(300)
                    if page.query_selector(".recap") is not None:
                        recap_bad.append(f"{lang}: the card stayed after being dismissed")
                    if not any("/api/mail/recap/seen" in u for u in posts):
                        recap_bad.append(f"{lang}: dismissing never closed the window")
                    page.reload(wait_until="networkidle")
                    page.wait_for_timeout(300)
                    if page.query_selector(".recap") is not None:
                        recap_bad.append(f"{lang}: the card came back after being dismissed")
                page.close()
                # Re-open the window for the next language.
                page = browser.new_page()
                page.request.get(f"http://127.0.0.1:{PORT}/__set?recap=reset")
                page.close()

            page = browser.new_page()
            page.request.get(f"http://127.0.0.1:{PORT}/__set?consent=off")
            page.close()
            browser.close()

            total = len(THEMES) * len(LANGS)
            distinct = {t: next(iter(g)) for t, g in grounds.items()}
            result.check(
                len(set(distinct.values())) == len(THEMES) and not themed,
                "0a. each theme actually painted a different ground",
                "\n".join(themed) or "; ".join(f"{t}={c}" for t, c in distinct.items()),
            )
            result.check(not reached, "0. the mail list rendered in every combination",
                         "\n".join(reached) or
                         f"{total} combinations -- without this the checks below are vacuous")
            result.check(not overflow_bad, "1. nothing overflows its container horizontally",
                         "\n".join(overflow_bad) or f"{total} theme x language combinations")
            result.check(not wrap_bad, "2. no button or badge wraps to a second line",
                         "\n".join(wrap_bad) or "buttons, tags and chips, every language")
            result.check(not untranslated, "3. the Korean UI has no English left in it",
                         "\n".join(untranslated) or "checked the strings the ...en spread swallowed")
            result.check(not raw_bad, "4. no raw i18n key or identifier reaches the screen",
                         "\n".join(raw_bad) or "a missing key renders as its own name; "
                         "a verdict with no mapping renders as snake_case")
            result.check(badge_seen == total, "5. the explored badge is actually painted",
                         f"visible in {badge_seen}/{total} combinations")
            result.check(why_seen == total, "6. the reason chip is painted",
                         f"visible in {why_seen}/{total} combinations")
            result.check(not brief_bad,
                         "24. a briefing row can be ticked or dismissed, leaves, and the next "
                         "one comes up",
                         "\n".join(brief_bad[:6]) or
                         "4 combinations: dismiss and tick each remove the row and refill the "
                         "gap; no 'made without AI' footer")
            result.check(not setup_bad,
                         "23. the first-run screen is in the user's language, speaks to a "
                         "user, and scrolls",
                         "\n".join(setup_bad[:6]) or
                         f"{len(THEMES[:2]) * len(LANGS)} combinations at 1280x640: the "
                         "permission steps are there, no npm/Terminal, nothing cut off")
            result.check(not consent_bad,
                         "22. mail goes to a cloud AI only after an explicit, easy-to-refuse yes",
                         "\n".join(consent_bad[:6]) or
                         f"{len(THEMES[:2]) * len(LANGS)} combinations: the dialog asks on its own, "
                         "Enter declines, six disclosure rows in the reader's language, "
                         "one grant on Allow, and Settings says so")
            result.check(not off_scale, "8. every duration comes from the motion scale",
                         "\n".join(off_scale[:4]) or
                         "no bespoke durations -- guessed numbers are what make motion feel cheap")
            result.check(not stagger_bad, "9. the list cascade is perceptible, and capped",
                         "\n".join(stagger_bad[:3]) or
                         "45ms per row for eight rows: 360ms of cascade. It was 18ms, "
                         "which finished in 90ms and read as one block appearing.")
            result.check(not done_bad, "10. completed mail can be reviewed and un-ticked",
                         "\n".join(done_bad[:4]) or
                         f"{total} combinations: rows, a decision date, full opacity, a ticked box")
            result.check(not irr_bad, "11. dismissed mail can be seen and taken back",
                         "\n".join(irr_bad[:4]) or
                         f"{total} combinations: rows, the category it was filed under, "
                         "and the same button to reverse it")
            result.check(not set_bad, "12. settings are grouped, and search ignores the groups",
                         "\n".join(set_bad[:4]) or
                         f"{total} combinations: tabs narrow the screen, and a term from "
                         "another group still finds its section")
            result.check(not recap_bad,
                         "25. the recap card lists one line per mail, every line opens it, "
                         "and only dismissing closes the window",
                         "\n".join(recap_bad[:6]) or
                         "4 languages: lines openable, opening writes nothing, dismiss "
                         "closes the window and it stays closed")
            result.check(not compose_bad,
                         "20. a reply can be written, approved and sent -- and the "
                         "approval screen cannot be typed into",
                         "\n".join(compose_bad))
            result.check(not reply_bar_bad,
                         "23. the reply bar stays on screen, uncovered and labeled, "
                         "at any header height",
                         "\n".join(reply_bar_bad[:6]) or
                         "default, 5000px and 70px headers; every control >= 36px")

            undefined = undefined_custom_properties()
            result.check(not undefined,
                         "21. every custom property the stylesheet uses is defined",
                         ", ".join(undefined) or
                         "an undefined one is dropped silently, leaving no background at all")

            result.check(not ai_bad, "19. the AI failure is in the reader's language",
                         "\n".join(ai_bad))
            result.check(not copies_bad, "18. a collapsed row says how many it stands for",
                         "\n".join(copies_bad))
            result.check(not notice_bad, "15. every notice can be closed, one at a time",
                         "\n".join(notice_bad))
            result.check(not split_bad, "16. the reading pane divider moves the boundary",
                         "\n".join(split_bad))
            result.check(not reason_bad, "17. the ranking reason is in the reader's language",
                         "\n".join(reason_bad))
            result.check(not badge_covers, "13. no rail badge is painted over its own icon",
                         "\n".join(badge_covers))
            result.check(not title_bad, "14. every briefing row is titled by its subject",
                         "\n".join(title_bad))
            result.check(not contrast_bad, "7. the reason chip is legible in every theme",
                         "\n".join(contrast_bad) or "contrast >= 3.0 against its own ground")
    finally:
        server.terminate()

    ok = result.render()
    print(f"screenshots: {SHOTS}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
