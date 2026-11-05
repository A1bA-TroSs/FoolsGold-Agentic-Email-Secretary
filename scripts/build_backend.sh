#!/usr/bin/env bash
# Freeze the Python backend into backend-dist/foolsgold-backend/.
#
# Run on the Mac you release from (Apple silicon builds an arm64 backend; an
# Intel build needs an Intel or universal2 Python). `npm run dist` calls this.
#
# A throwaway venv, not backend/.venv: the release must be built from exactly
# requirements.txt, not from whatever has accumulated in the dev environment.
set -euo pipefail
cd "$(dirname "$0")/.."

# python.org's Python, not Homebrew's. Homebrew builds every binary for the
# macOS it was installed on, and PyInstaller copies those binaries into the
# app -- so a backend frozen with Homebrew Python on macOS 15 refuses to load
# on macOS 14 or older. It ran on the build Mac and would have failed on a
# tester's. python.org builds target macOS 11. (PyInstaller maintainers'
# advice: github.com/orgs/pyinstaller/discussions/9143)
PYORG=/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13
if [ -z "${FOOLSGOLD_BUILD_PYTHON:-}" ] && [ -x "$PYORG" ]; then
  PY="$PYORG"
else
  PY="${FOOLSGOLD_BUILD_PYTHON:-python3.13}"
fi
command -v "$PY" >/dev/null || { echo "need $PY (set FOOLSGOLD_BUILD_PYTHON)"; exit 1; }
PY_REAL="$("$PY" -c 'import sys, os; print(os.path.realpath(sys.executable))')"
case "$PY_REAL" in
  /opt/homebrew/*|/usr/local/Cellar/*|/usr/local/opt/*)
    if [ "${FOOLSGOLD_ALLOW_HOMEBREW:-}" != "1" ]; then
      echo "Refusing to freeze with Homebrew Python ($PY_REAL)."
      echo "The result would only run on this macOS version or newer."
      echo "Install Python 3.13 from https://www.python.org/downloads/macos/ and run again,"
      echo "or set FOOLSGOLD_ALLOW_HOMEBREW=1 for a build that stays on this Mac."
      exit 1
    fi ;;
esac
echo "freezing with $PY_REAL"

BUILD_VENV="$(mktemp -d)/venv"
"$PY" -m venv "$BUILD_VENV"
"$BUILD_VENV/bin/pip" install --quiet --upgrade pip
"$BUILD_VENV/bin/pip" install --quiet -r backend/requirements.txt pyinstaller

rm -rf backend-dist
( cd backend && "$BUILD_VENV/bin/pyinstaller" --noconfirm --clean \
    --distpath ../backend-dist --workpath ../.pyinstaller-work foolsgold-backend.spec )
rm -rf .pyinstaller-work

BIN="backend-dist/foolsgold-backend/foolsgold-backend"
test -x "$BIN" || { echo "freeze produced no executable at $BIN"; exit 1; }

# Smoke test: the frozen server must actually start and answer. A freeze that
# builds but misses a lazily imported module only fails here -- or in a user's
# hands. Port 8799 so a running dev backend on 8765 is not disturbed.
#
# The wait is generous on purpose. The first launch of a freshly built,
# unsigned 50 MB onedir is slow on macOS: every .dylib/.so in _internal/ is
# checked by the system on first load, so "Application startup complete" can
# land well after 20 s -- which is exactly what the original 40 x 0.5 s loop
# reported as a failure (2026-09-21, on a binary that was fine). So:
#   * up to FOOLSGOLD_SMOKE_TIMEOUT seconds (default 120), polling once a second;
#   * stop at once if the process dies -- a real failure should not wait 2 min;
#   * run it twice and print both times: the cold number is what a user sees
#     on first launch, the warm number on every launch after. Electron waits
#     for the backend too (electron/main.js waitForBackend), so these numbers
#     are what that timeout has to cover.
SMOKE_TIMEOUT="${FOOLSGOLD_SMOKE_TIMEOUT:-120}"
LOG=/tmp/foolsgold-freeze.log
PID=""
cleanup() {   # never fails: it runs under `set -e` and from the EXIT trap
  if [ -n "$PID" ]; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  PID=""
}
trap cleanup EXIT

smoke() {  # $1 = label; prints seconds taken, returns non-zero on failure
  local label="$1" home start now
  home="$(mktemp -d)"
  start=$(date +%s)
  FOOLSGOLD_PORT=8799 FOOLSGOLD_HOME="$home" "$BIN" >"$LOG" 2>&1 &
  PID=$!
  while :; do
    if curl -fsS --max-time 30 http://127.0.0.1:8799/api/health >/dev/null 2>&1; then
      now=$(date +%s)
      echo "frozen backend answered /api/health ($label start: $((now - start))s)"
      cleanup
      return 0
    fi
    if ! kill -0 "$PID" 2>/dev/null; then
      echo "frozen backend EXITED before answering ($label start) -- log:"
      tail -30 "$LOG"
      PID=""
      return 1
    fi
    now=$(date +%s)
    if [ $((now - start)) -ge "$SMOKE_TIMEOUT" ]; then
      echo "frozen backend did not answer /api/health within ${SMOKE_TIMEOUT}s ($label start) -- log:"
      tail -30 "$LOG"
      return 1
    fi
    sleep 1
  done
}

smoke cold
smoke warm

# Which macOS the frozen backend actually needs: the highest minimum-OS of any
# binary it carries. This is the number a tester's Mac has to meet.
if command -v otool >/dev/null; then
  MIN=$(find backend-dist -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) -print0 \
        | xargs -0 otool -l 2>/dev/null \
        | awk '/LC_BUILD_VERSION/{b=1} b&&/minos/{print $2; b=0} /LC_VERSION_MIN_MACOSX/{v=1} v&&/version/{print $2; v=0}' \
        | sort -t. -k1,1n -k2,2n | tail -1)
  echo "frozen backend needs macOS ${MIN:-unknown} or newer"
fi
echo "frozen backend OK: $BIN"
