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
4. Python 3.13 on PATH as `python3.13` (or set `FOOLSGOLD_BUILD_PYTHON`).

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

- [ ] **Upgrade Electron.** 32 has been end-of-life since 2025-03-04 and this
      app renders untrusted HTML mail. Move to a supported major (42+; 43 or
      44 preferred), update `electron-builder` to match, run
      `npm run test:electron` and click through the app.
- [ ] Fill the `[TODO]`s in `docs/PRIVACY.md` (publisher name, address,
      support email) and publish it at a stable URL; link it from the site's
      download page.
- [ ] Bump `version` in `package.json` and `backend/app/config.py`.
- [ ] Decide auto-update: none for 1.0 (users re-download) is acceptable.

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
- **Export**: a DMG from our own site is not an App Store export declaration;
  the app uses only standard encryption (TLS, Fernet) for its own data.
