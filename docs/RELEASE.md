# Releasing FoolsGold (notarised DMG, direct download)

Approved strategy (2026-09-21): **Developer ID-signed, notarised DMG distributed
from our own website.** Not the Mac App Store: its sandbox (guideline 2.5.2)
forbids reading `~/Library/Mail`, which is the Apple Mail source. Notarisation
is Apple's automated malware scan, not App Review; it usually finishes in
minutes. Research and sources: project doc `claude/app-store-release.md`.

## One-time setup (on the Mac that builds)

1. Apple Developer Program membership ($99/yr).
2. In Xcode > Settings > Accounts, or developer.apple.com > Certificates,
   create a **Developer ID Application** certificate and install it in the
   login keychain. Check: `security find-identity -v -p codesigning` lists
   `Developer ID Application: <name> (<TEAMID>)`.
3. Notarisation credentials, one of:
   - an app-specific password (appleid.apple.com > Sign-In and Security), or
   - an App Store Connect API key (.p8) with Developer access.
4. **Python 3.13 from python.org — not Homebrew.** `scripts/build_backend.sh`
   now *refuses* a Homebrew interpreter, because PyInstaller copies its binaries
   into the app and Homebrew builds them for the macOS it was installed on: the
   result runs on the build Mac and fails to load on any older one. This is a
   blocking step, and on this Mac the only 3.13 today is Homebrew's
   (`backend/.venv` points at `/opt/homebrew/opt/python@3.13`).

   ```bash
   # install from https://www.python.org/downloads/macos/ then check:
   /Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13 -V
   ```

   The script picks that path automatically when it exists. `FOOLSGOLD_BUILD_PYTHON`
   overrides it; `FOOLSGOLD_ALLOW_HOMEBREW=1` forces a build that must never be
   shipped to anyone else. After the freeze the script prints the minimum macOS
   each binary actually requires (`otool -l`) — read it, and make sure it is not
   higher than `build.mac.minimumSystemVersion` in `package.json` (12.0).

## Every release

```bash
# credentials for notarisation -- never commit these
export APPLE_ID="you@example.com"
export APPLE_APP_SPECIFIC_PASSWORD="abcd-efgh-ijkl-mnop"
export APPLE_TEAM_ID="ABCDE12345"
# (or: APPLE_API_KEY=/path/AuthKey_XXXX.p8 APPLE_API_KEY_ID=XXXX APPLE_API_ISSUER=<uuid>)

npm ci                      # clean install from package-lock
npm run test:electron
(cd backend && .venv/bin/python -m pytest -q)
npm run dist                # UI build -> frozen backend -> signed, notarised DMG
```

`npm run dist` runs `build:ui`, then `scripts/build_backend.sh` (PyInstaller
freeze + a `/api/health` smoke test on port 8799), then `electron-builder
--mac dmg --arm64`, which signs every Mach-O file in the bundle (including the
frozen backend under `Contents/Resources/backend/`) with hardened runtime and
the entitlements in `packaging/`, submits to notarytool, and staples.

## Verify before uploading

```bash
APP="dist/mac-arm64/Fools Gold.app"
codesign --verify --deep --strict --verbose=2 "$APP"
spctl -a -vv "$APP"                       # expect: accepted, source=Notarized Developer ID
xcrun stapler validate dist/*.dmg
codesign -d --entitlements - "$APP/Contents/Resources/backend/foolsgold-backend/foolsgold-backend"
```

Then on a **second Mac or a fresh user account** (no Homebrew, no venv):
first launch opens without a Gatekeeper warning; Full Disk Access prompt and
Apple Mail source work; with Ollama absent the app still ranks; quitting the
app leaves no `foolsgold-backend` process (`pgrep -fl foolsgold`).

## Before the first public release

- [x] **Upgrade Electron.** Done: `^43.0.0` (43.7.3 installed) with
      `electron-builder ^26.15.3`. Stay on 43 unless `minimumSystemVersion` is
      raised with it — 44 drops macOS 12, which `package.json` still promises.
- [ ] Fill the `[TODO]`s in `docs/PRIVACY.md` (publisher name, address,
      support email) and publish it at a stable URL; link it from the site's
      download page.
- [x] Bump `version` in `package.json` and `backend/app/config.py`. Both are
      **1.2.7** as of 2026-09-24 — matching the commit that shipped the recap. They had drifted apart and away from the git
      history: `package.json` said 1.0.0, `config.py` said `1.0.0-dev`, and the
      last tagged commit was v1.2.6 — so the About box and the DMG filename
      would both have shipped a version that never existed. Keep them equal;
      `config.py` is what `/api/settings` reports to the UI.
- [ ] Decide auto-update: none for 1.0 (users re-download) is acceptable.

## 1.2.7 — what changed that a release needs to know about

- **The recap card** ("piled up"): a pre-read summary of what arrived since the
  user last looked, one line per mail, sectioned by category, with its own rail
  entry. No new outbound data path — see the compliance note below.
- **Schema migration, additive and automatic.** `classifications.summary` is
  added on first start; `classifications.category` is now actually persisted
  (it never was — the INSERT omitted the column while the caller passed it, so
  every row written before 1.2.7 has an empty category). Nothing needs to be
  done at upgrade time, but **existing mail keeps an empty category and an
  empty summary until it is re-classified**, so a tester's first recap will be
  thinner than a fresh install's. There is no backfill command yet.
- **Nothing in the app marks mail as read.** `is_read` comes from Apple Mail.
  The recap relies on that, so a tester who reads mail elsewhere will correctly
  see a smaller pile.

## Compliance notes

- **Cloud AI consent** (Apple 5.1.2(i) spirit, Korea PIPA overseas transfer):
  enforced in `backend/app/llm/registry.get_provider()` via `app/consent.py`;
  UI in `ConsentDialog.jsx`; tests in `backend/tests/test_ai_consent.py` and
  render check 22. Bump `consent.DISCLOSURE_VERSION` whenever what is sent or
  to whom changes -- every user is then asked again.
- **Korea AI Basic Act** (in force 2026-01-22): users are told the product
  uses AI (Settings, consent dialog), and generated output must be labelled
  as AI-made. [ ] Check that the briefing and AI-written ranking reasons carry
  a visible "AI" label before release.
- **The per-email `summary`** added in 1.2.7 is produced by the *same*
  classification call that already runs on every message, so it opens no new
  outbound path and needs no `DISCLOSURE_VERSION` bump: a cloud provider that
  was already reading the mail to bucket it now also returns one sentence about
  it. If that ever becomes a separate call, or goes to a different recipient,
  the disclosure version must move.
- **Export**: a DMG from our own site is not an App Store export declaration;
  the app uses only standard encryption (TLS, Fernet) for its own data.
