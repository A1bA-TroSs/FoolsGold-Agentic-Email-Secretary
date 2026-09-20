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
