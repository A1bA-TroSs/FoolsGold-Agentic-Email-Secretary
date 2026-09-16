# Fools Gold on iPhone — the plan (2026-09-15)

Decision taken this session: **a full on-device rewrite in Expo / React Native,
proven to run before anything is decided about shipping it.** That is the
expensive option and it was chosen over the cheap one deliberately. This
document records what it costs, what survives, and what cannot come.

Register note: written in the register of the other project docs. The vault's
own style guide was not readable this session — `~/projects` is not a connected
folder — so this is the house voice as observed, not as specified.

---

## What was rejected, and why it matters

The cheap path was real and was on the table: keep the FastAPI backend running
on the Mac, reach it over Tailscale, make the phone a thin client. It reuses
**5,092 lines of Python, 209 tests, the Apple Mail source and the one LLM
provider that has ever actually worked.** It could have been standing up inside
a day.

It was rejected. The phone would have been a remote control for a Mac that has
to be awake — and an email secretary you cannot consult on a train is not the
thing. The rewrite is chosen because the artifact has to be self-contained, and
that is worth paying 5,000 lines for.

**So the cost is stated plainly at the top: almost none of the backend crosses
over.** What crosses over is the *knowledge* in it, and the knowledge is worth
more than the code.

---

## What cannot come with us

| Thing | Lines | Why it dies |
| --- | --- | --- |
| `sources/applemail.py` | 607 | iOS sandboxes every app away from every other app's store. There is no `~/Library/Mail` and there is no entitlement that creates one. This is not hard, it is impossible. |
| `llm/copilot_provider.py` | 165 | `github-copilot-sdk` is Python, needs a keychain-held OAuth device-flow session, and has no client-side equivalent. |
| `graph/` + `sources/graph_source.py` | 368 | Blocked on the desktop because Danny's tenant forbids app registration. Nothing about moving to a phone changes a tenant policy. |
| `crypto.py` (Fernet, key at `~/.foolsgold/secret.key`) | 49 | Replaced by the iOS Keychain through `expo-secure-store`. This is an upgrade, not a loss — the deferred item "Fernet key, not macOS Keychain" is resolved by the platform. |
| `electron/reminders.js` | — | Replaced by `expo-notifications`. Also mostly an upgrade; see the one part that gets *worse* below. |

### The Copilot loss is the serious one

`release-readiness.md` is blunt about it: Copilot is **"called constantly and
working — 979 of 993 emails"**, and Anthropic and OpenAI have **"never made a
request. No request has ever left the machine."**

Moving to mobile deletes the only provider that has been proven and forces the
app onto the two that have not. That is a risk, but it is also the answer to
item 2 of "Still to do before a release". **Mobile is the excuse to finally
prove an API-key provider end to end** — and it should be proven on the desktop
first, where there is a working oracle to compare against, not on the phone
where every result would be new.

---

## What the mailbox becomes

**Gmail API over HTTPS.** Not a preference — an elimination.

- **IMAP is out.** React Native has no TCP sockets. IMAP needs a native module,
  which needs a custom dev build, which is a native dependency added in week
  one for a protocol that then still needs OAuth 2.0 (`XOAUTH2`) because basic
  auth is gone. Gmail's REST API is `fetch`, and `fetch` is already there.
- **Graph is out** for the tenant reason above.
- So **the phone is a Gmail app.** `yskingpark@gmail.com` works.
  `connect.ust.hk` does not, and will not, until the tenant changes. If UST
  mail turns out to be a requirement rather than a nice-to-have, **the
  on-device decision itself should be reopened** — that is the one finding that
  could overturn today's choice.

### Auth

`expo-auth-session` with PKCE, installed-app flow. No client secret, no
backend, no server to hold a token. Refresh token in `expo-secure-store`
(iOS Keychain).

`gmail.readonly` is a **restricted scope**, which normally drags in an annual
CASA security assessment. Google's own exceptions cover this case twice over:
apps whose users are "only a few users, all of whom are known personally to
you", and apps left in **Testing** publishing status. The costs land only if
this ever goes public *through a third-party server* — and there is no server.
An unverified app shows a tester warning screen and has a capped refresh-token
lifetime. Both acceptable.

### Sync

`users.messages.list` for the first pull, `users.history.list` for every pull
after. **Not a timestamp scan** — bug 7 on the desktop was exactly the lesson
that mtime is useless for freshness, and Gmail hands us a real history cursor,
so do not reinvent the two-pass scan.

---

## What the model becomes

Two providers, in this order, and the order is the point.

**1. Bring your own key (Anthropic / OpenAI), HTTPS from the device.**
React Native is not a browser, so there is no CORS problem. The key lives in
the Keychain. This path exists first because it is the *known* shape — the same
prompt, the same JSON, the same `stop_reason` check — so any difference in
output is the model's, not the plumbing's.

**2. Apple Foundation Models, on device.**
~3B parameters, iOS 26+, free, private, offline, no key at all. Structured
output through `DynamicGenerationSchema` constrained decoding — **guaranteed
valid JSON**, which removes an entire class of failure the desktop had to
defend against (`max_tokens: 4096` truncating a batch into unparseable JSON
that looked exactly like a rejected key). Reachable from RN through
`expo-local-llm` or `react-native-apple-llm`; both need a dev build, neither
works in Expo Go.

The classifier returns `{bucket, tasks: [{title, due}]}`. That is a small flat
object, which is precisely what guided generation on a small model is good at.
Keep it flat — schema complexity limits on the 3B model are real and
undocumented.

**The risk to watch is comprehension, not format.** The Co-op progress report
failure was a truncation, not a model failure — but a 3B model reading a
flattened HTML table, matching the *n*th date to the *n*th label, is a harder
ask than a frontier model doing it. `_body_for_prompt()`'s window logic must
port literally, and the table-reading instruction in the prompt must come with
it. If the on-device model gets that email wrong after a correct body reaches
it, that is a real finding about model size, and worth recording as one.

---

## The seam: keep `api.js`

The single highest-leverage architectural instruction in this document.

`frontend/src/lib/api.js` is 94 lines and about 40 functions, and every one of
them is a name plus arguments. `App.jsx` (828 lines) calls those names and
knows nothing else. Today each function is a `fetch`.

**Reimplement those same 40 functions against SQLite and Gmail, keep the
signatures byte-identical, and every caller ports unchanged.** The HTTP layer
disappears without any screen noticing. Do not let the rewrite "simplify" this
into direct database calls from components — that boundary is the only reason
the port is tractable.

Two other files port nearly verbatim:

| File | Lines | Change needed |
| --- | --- | --- |
| `lib/due.js` | 45 | none. Pure date arithmetic, no DOM. `FORGET_AFTER_DAYS = 30` comes across intact. |
| `lib/i18n.js` | 1,068 | none in content; `createContext`/`useContext` work identically in RN. Four languages survive free. |
| `lib/useFlip.js` | 59 | **rewrite.** It is a FLIP animation over DOM geometry. Becomes `react-native-reanimated` layout animations. |

---

## The port list, by weight

| Python | Lines | Becomes | Difficulty |
| --- | --- | --- | --- |
| `priority.py` | 459 | `lib/priority.ts` | **Easiest.** Pure scoring functions with no I/O, and the desktop is a working oracle: run both against the same mailbox and diff the rankings. Port this first, not last. |
| `planner.py` | 336 | `lib/planner.ts` | Medium. The six-row month grid moves from Python into the client, which is fine — but `todayIso()` stays local-time. Never `toISOString()`. |
| `db.py` | 880 | Drizzle schema + queries over `expo-sqlite` | Hard, and the hardest part is not SQL. `tasks.dedup_key` and `task_sources(task_id, email_id, source_key)` must survive **exactly** — keyed by what the mail said, not by the row's current wording. That distinction is what stops a user's rename being read as an abandoned commitment. It cost a real bug to find. |
| `pipeline.py` | 544 | `lib/pipeline.ts` | Hard. Bring `_upgrade_structural()` and the rate-limit backoff with it. A first sync on a new key classifying a few hundred messages is *the expected case*, not the edge case. |
| `llm/base.py` | 434 | `lib/llm/prompt.ts` | **The prompt is the asset.** `_body_for_prompt()` — first 2,000 characters whole, then marked windows around later dates to a 1,600-character budget — is a fix that took weeks to locate. Port it literally, comments included. |
| `config.py`, `llm/registry.py` | 173 | settings module | Easy. |

Roughly **2,400 lines of genuinely hard logic** and about **1,600 lines that are
deleted or replaced by platform features.**

---

## Screens

| Desktop | iPhone |
| --- | --- |
| Left rail: Priority / All mail / Calendar / Muted | Bottom tab bar, same four. Priority keeps the agenda icon. |
| Two panes, list + detail | Stack navigation: list pushes detail. `expo-router`. |
| `Digest.jsx` in the right pane | Header block on the Priority tab, above the list. |
| `Settings.jsx` (391 lines) | Its own screen off the tab bar, sectioned. |
| `MailDetail.jsx` iframe, `sandbox=""` | `react-native-webview`, `javaScriptEnabled={false}`, `originWhitelist={[]}`. **The rule from bug 13 stands: never grant same-origin.** Height is measured by injected script or fixed — not by reading the document. |
| Hover affordances (mute, highlight, pin) | Swipe actions and long-press. This is the one place the interaction model genuinely changes rather than rearranges. |

`styles.css` (1,517 lines) does not port. The four themes and the six highlight
colours port as a token object — and that mapping was already the design
(`db.HIGHLIGHT_COLORS` → theme tokens, never raw hex), so the work is
mechanical rather than a redesign.

**Bug 28 cannot happen in RN** — there are no inheriting CSS custom properties,
so a swatch cannot silently take an ancestor's colour. The rendering-differences
class of bug mostly disappears with the stylesheet. That is a real and
underrated benefit of the rewrite.

---

## Background work and the one thing that gets worse

`expo-background-task` over `BGAppRefreshTask`: runs opportunistically, **every
1–6 hours in practice**, `minimumInterval` is a floor and not a promise,
rarely-opened apps get demoted until it effectively stops firing, and a
force-quit stops it entirely with no workaround. Silent push is worse for this
purpose and needs a server we do not have.

**So: background sync is a bonus, foreground open is the real sync.** Design for
"opening the app is fast and current", not for "the phone kept up overnight".

The daily reminder mostly improves — iOS schedules a local notification and it
fires whether or not the app is alive, which is strictly better than a
one-minute ticker in an Electron process. But one rule breaks:

> **"Nothing sent on an empty day."**

A scheduled local notification fires *without running our code*, so emptiness
cannot be checked at fire time. The fix is to invert it: the evening background
task computes tomorrow's agenda and **schedules or cancels** the notification
accordingly. If the background task did not run, the notification is stale.
Accept a stale reminder, or accept a missing one — that is an open design
question, not a solved one, and it should be decided rather than discovered.

---

## Skills — who needs to know what

| Thing | Who | Why |
| --- | --- | --- |
| Expo Go on the iPhone, Metro on the Mac, same Wi-Fi | **Danny** | This is the whole of phase 0. Scan a QR code. |
| Reading a native build error | **Danny**, eventually | The first one is always confusing and always a config plugin. |
| Google Cloud console: OAuth client, Testing status, own account as test user | **Danny** | Requires his Google account; cannot be done for him. |
| Free Apple ID provisioning in Xcode (7-day builds) | **Danny**, at phase 3 | Only when the dev build is needed for Foundation Models. |
| Expo Router, NativeWind, Drizzle, `expo-sqlite`, `expo-secure-store`, `expo-auth-session`, `expo-notifications`, Reanimated | Claude | |
| Porting 2,400 lines of Python to TypeScript | Claude | |
| Writing the tests that prove the port matches the desktop | Claude | |

**Deliberately not needed:**

- **Swift / Xcode project surgery** — not for phases 0–2. Foundation Models
  arrives through an existing Expo module, not a hand-written bridge.
- **Supabase / Firebase / any backend.** The template the research surfaced
  (`vibecode/mobile-template-1`) and most vibe-coding mobile advice assumes a
  hosted backend, because most such apps are multi-user. This one is
  single-user and local-first by design. **Ignore that advice; it is the
  largest piece of unnecessary complexity on offer.**
- **Redux / Zustand.** The desktop holds all state in `useState` across 828
  lines of `App.jsx` and it held. There is no server state to cache.

---

## Phases

**P0 — Does it run.** *(the milestone asked for this session)*
Expo Go on the phone, bottom tabs, hardcoded fake mail, one theme.
No OAuth, no database, no model. Proves the toolchain on Danny's actual
hardware and nothing else. If this is painful, everything after it is worse,
and that is worth knowing in an afternoon rather than a month.

**P1 — A mailbox.** Gmail OAuth, message list, detail view, `expo-sqlite`
store. No ranking, no AI. Mail in date order.

**P2 — Judgement without a model.** Port `priority.py`. Structural ranking,
buckets, mute, highlight. **Diff the ranking against the desktop on the same
mailbox** — this is the only phase with a free oracle, so spend it.

**P3 — A model.** BYO key first (and prove that path on the desktop in
parallel, resolving readiness item 2). Then Foundation Models behind the same
interface, and compare the two on the same 993 emails.

**P4 — Calendar and to-dos.** `planner.py`, the month grid, `dedup_key`,
`task_sources`, the removed box, the forget horizon.

**P5 — Notifications.** Daily reminder plus the schedule-or-cancel design above.

**P6 — Distribution.** Deferred by decision. Revisit only when P0–P5 work.

---

## What we will not know until we try

Stated honestly, in the manner of the readiness pass.

- Whether a 3B on-device model can classify real mail usefully. Nobody knows.
  The 993-email corpus makes this **answerable**, which is unusual and valuable.
- Whether Gmail's `history.list` behaves for a mailbox this size on a phone's
  network.
- Whether `expo-sqlite` is comfortable with the full message bodies. It should
  be; it has not been measured.
- Whether the swipe-and-long-press interaction model is actually usable, or
  whether mute/highlight/pin needs a different shape entirely on a small
  screen. This is a design risk, not a technical one, and it is probably the
  likeliest thing to force a rewrite of a screen.
- Whether two codebases drift. `rendering-differences.md` is a whole document
  about two engines drifting apart on one rule. Two *languages* will drift
  faster. **Proposal: one shared JSON file holding `FORGET_AFTER_DAYS`, the
  scoring curve constants and the classifier prompt, read by both trees.**
  Decide this in P2, before there is anything to reconcile.

---

## Open decisions

1. **Is UST mail a requirement?** If yes, on-device may not survive the answer.
2. **Do desktop and mobile share constants and the prompt, or fork?** Decide
   before P2.
3. **Two databases, no sync** — accepted, or is sync in scope? Accepting it
   means a mute on the phone does not mute on the Mac.
4. **Stale reminder or missing reminder**, per the notification problem above.
