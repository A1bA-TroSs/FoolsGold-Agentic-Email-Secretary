# Fools Gold — v1.0 Design: Email Secretary (Summarization)

## Vision

Fools Gold is a local-first, agentic AI email assistant — an "AI-native email client," not a triage tool bolted onto Outlook. The full product vision spans three versioned chapters:

- **1.0 — Email Secretary (Summarization)**: read, organize, prioritize, and summarize the inbox. No writes.
- **2.0 — Calendar + Scheduling**: a built-in calendar, auto-marking events/deadlines pulled from emails, schedule management.
- **3.0 — Live Agentic Assistant**: read-write chat interface, drafting/sending replies, full automation (e.g. "book a restaurant" from a natural-language request to a brand-new recipient), with confirm-before-send as a permanent safety rail — never a fully autonomous send, especially to new contacts.

This document specs **1.0 only**. It is the smallest slice that already feels like the dream product, not a stripped-down demo: it should be genuinely useful to open daily.

## Background / Motivation

Built by a first-time shipper with a Python background and AI-assisted ("vibe coding") workflow, targeting a consistent 2–3 hour window each evening after work. The project is scoped deliberately narrow for v1.0 so it can actually ship within a few weeks of evening sessions, rather than stalling in setup/plumbing.

Target inbox: **Outlook** (Microsoft 365 / Outlook.com) — this is where the actual daily pain is (school + work), even though Gmail is the more commonly-built-for ecosystem.

The core problem being solved: priority in a real inbox is contextual and time-varying (e.g. a professor matters less once the course ends), not a static importance ranking of senders. Real executive assistants triage against *what you're currently working on*, sort mail into actionable vs. informational vs. reference-noise, and weight urgency by deadline proximity. This design mirrors that.

## Scope

**In scope for v1.0:**
- Outlook OAuth login via Microsoft Graph API
- Fetching and locally caching mail (metadata + body)
- Dashboard: prioritized/highlighted email list + full email detail view
- Priority engine: user-maintained "active priorities" list, with system-suggested candidate topics surfaced on demand (derived from the last 1–2 months of mail activity) for the user to promote/demote/ignore
- Per-email classification into **Action needed / FYI / Reference-noise**, plus deadline extraction
- Daily AI-generated summary ("what's crucial today")
- Settings panel: priority list management, LLM provider + API key configuration, theme switcher

**Explicitly out of scope for v1.0** (deferred to 2.0/3.0):
- Calendar event creation or any calendar UI
- Replying, composing, or sending email
- Any write access to the mailbox
- Multi-provider support (Gmail/other) — Outlook only
- Multi-user/hosted deployment — local-first only, single user

## Approaches Considered

**Platform:** local-first desktop-run web app (chosen) vs. hosted multi-user web app vs. downloadable packaged desktop app. Local-first wins for v1.0: zero hosting/deployment complexity, and — critically for a tool reading work/school inbox content — email data and OAuth tokens never leave the user's machine. Hosted/packaged distribution is a decision to revisit once 1.0 is proven useful to its one user.

**Stack:** Python (FastAPI) + React (chosen) vs. all-Python full-stack (e.g. Reflex/Streamlit) vs. FastAPI + server-rendered HTML (HTMX/Jinja). FastAPI + React was chosen because 2.0 (custom calendar widget) and 3.0 (live chat interface) both need a genuinely interactive frontend; starting there avoids a framework migration later, and the user's AI-assisted workflow makes picking up React low-friction despite no prior experience with it.

**LLM provider:** pluggable adapter (chosen) vs. hardcoded single vendor. The user should be free to bring whichever provider fits their budget/privacy preference. v1.0 ships adapters for Claude and OpenAI; local-model support (e.g. via Ollama) is a natural later addition once the adapter interface exists.

## Architecture

- **Backend:** Python + FastAPI, run locally (`localhost`). Responsible for Microsoft Graph auth/sync, local persistence, and calling the configured LLM provider.
- **Frontend:** React SPA served locally. Obsidian-web-clipper-inspired panel layout: a thin icon rail, an email list pane, and a detail pane. Theming via CSS custom properties, four palettes: **Gold & Ivory (default)**, Dark, Green, Purple.
- **Storage:** local SQLite. Tables (indicative, not final): `emails` (Graph message id, headers, body, folder, timestamps), `priorities` (user-managed topic list with status: active/low-care/dismissed), `classifications` (email id → bucket, extracted deadline, priority score, model/version used), `settings` (LLM provider + encrypted API key, active theme), `oauth_tokens` (encrypted access/refresh tokens).
- **LLM provider layer:** a thin adapter interface —
  - `classify(email) -> { bucket: "action" | "fyi" | "noise", deadline: date | null, rationale: string }`
  - `summarize(emails: list) -> digest: string`
  Implementations: `ClaudeProvider`, `OpenAIProvider` for v1.0.
- **Branding:** name "Fools Gold," logo at `assets/logo.png` (gold low-poly crystal cluster with orbit ring, ivory rounded-square background) — visually the source for the Gold & Ivory theme's palette and the icon-rail accent color.

## Data Flow

1. **Auth:** user logs into Outlook via Microsoft Graph OAuth; access/refresh tokens stored encrypted locally.
2. **Sync:** manual "refresh" action plus periodic polling while the app is open; fetched mail is upserted into the local `emails` cache.
3. **Classification:** new/changed emails are sent to the configured LLM provider's `classify()` — returns actionability bucket + extracted deadline; result is scored against the user's current active-priorities list to produce a priority score.
4. **Dashboard render:** email list sorted/highlighted by priority score and bucket; detail pane shows full email content.
5. **Digest:** `summarize()` runs automatically once per calendar day (on first dashboard open that day) over the current prioritized set to produce the "what's crucial today" text; also manually re-triggerable on demand.
6. **Priority maintenance:** settings panel surfaces newly-detected candidate topics (recurring senders/subjects/threads from the last 1–2 months not yet classified by the user) for promotion/demotion — triggered on demand, never a forced prompt.

## Error Handling

- **Graph token expiry:** silent refresh using the stored refresh token; re-prompt login only if refresh itself fails.
- **LLM call failure:** fall back to structural signals only (To vs. CC, has-attachment, known sender) so the list still sorts sensibly; surface a visible "AI unavailable" badge rather than blocking the UI.
- **Rate limiting (Graph or LLM):** exponential backoff with a retry queue; never silently drop a fetched email.

## Testing

Scoped proportionally to a first solo ship:
- Unit tests around priority-scoring logic and LLM-response parsing (the parts most likely to silently misbehave and hardest to catch by eye)
- Manual end-to-end verification of OAuth login and the core dashboard flow — full browser-automation suites are not warranted at this scope

## Open Questions for Later Chapters

- 2.0: how does the built-in calendar reconcile with Outlook's own calendar (mirror vs. independent)?
- 3.0: exact confirm-before-send UX, and how "already-known contact" vs. "brand-new recipient" is determined for staged trust levels.
- Whether/when to package as a downloadable desktop app (Tauri) or move to hosted multi-user, once 1.0 is validated for personal use.
