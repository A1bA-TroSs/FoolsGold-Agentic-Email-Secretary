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

PY="${FOOLSGOLD_BUILD_PYTHON:-python3.13}"
command -v "$PY" >/dev/null || { echo "need $PY (set FOOLSGOLD_BUILD_PYTHON)"; exit 1; }

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
FOOLSGOLD_PORT=8799 FOOLSGOLD_HOME="$(mktemp -d)" "$BIN" >/tmp/foolsgold-freeze.log 2>&1 &
PID=$!
for _ in $(seq 1 40); do
  if curl -fsS http://127.0.0.1:8799/api/health >/dev/null 2>&1; then
    kill "$PID"; wait "$PID" 2>/dev/null || true
    echo "frozen backend OK: $BIN"
    exit 0
  fi
  sleep 0.5
done
kill "$PID" 2>/dev/null || true
echo "frozen backend did not answer /api/health -- see /tmp/foolsgold-freeze.log"
tail -20 /tmp/foolsgold-freeze.log
exit 1
