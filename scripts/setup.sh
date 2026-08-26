#!/usr/bin/env bash
# One-time setup. Run from the repo root:  ./scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

MIN_MINOR=11   # github-copilot-sdk needs 3.11; FastAPI route hints need >= 3.10

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
die()  { printf '\033[31m%s\033[0m\n' "$1" >&2; exit 1; }

# --------------------------------------------------------------------------
# Find a usable Python.
#
# macOS ships /usr/bin/python3 as 3.9, and `python3 -m venv` will happily build
# a 3.9 environment that then fails halfway through pip with a wall of
# "Requires-Python >=3.11" lines that does not obviously say "wrong Python".
# So check up front and say it plainly.
# --------------------------------------------------------------------------
usable() {
  [ -x "$(command -v "$1" 2>/dev/null)" ] || return 1
  "$1" -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, $MIN_MINOR) else 1)" 2>/dev/null
}

PYTHON=""
if [ -n "${FOOLSGOLD_PYTHON:-}" ]; then
  usable "$FOOLSGOLD_PYTHON" \
    || die "FOOLSGOLD_PYTHON=$FOOLSGOLD_PYTHON is not Python 3.$MIN_MINOR or newer."
  PYTHON="$FOOLSGOLD_PYTHON"
else
  for candidate in python3.14 python3.13 python3.12 python3.11 python3 \
                   /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if usable "$candidate"; then PYTHON="$(command -v "$candidate")"; break; fi
  done
fi

if [ -z "$PYTHON" ]; then
  found="$(python3 --version 2>&1 || echo 'none found')"
  cat >&2 <<MSG

$(printf '\033[31mFool'"'"'s Gold needs Python 3.%s or newer.\033[0m' "$MIN_MINOR")

  Your default python3 is: $found
  macOS ships 3.9, which is too old — the Copilot SDK requires 3.11, and
  FastAPI's route type hints need 3.10+.

  Install a newer one:

    brew install python@3.13

  or download an installer from https://www.python.org/downloads/macos/

  Then run this script again. To point it at a specific interpreter:

    FOOLSGOLD_PYTHON=/opt/homebrew/bin/python3.13 ./scripts/setup.sh

MSG
  exit 1
fi

PY_VERSION="$("$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
bold "==> Python backend  (using $PYTHON — $PY_VERSION)"

# An existing venv built with the wrong interpreter is the exact trap this
# script is here to prevent, so replace it rather than installing into it.
if [ -d backend/.venv ]; then
  existing="$(backend/.venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "?")"
  wanted="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  if [ "$existing" != "$wanted" ]; then
    echo "    Existing venv is Python $existing, rebuilding it as $wanted."
    rm -rf backend/.venv
  fi
fi

"$PYTHON" -m venv backend/.venv
backend/.venv/bin/pip install --upgrade pip --quiet
backend/.venv/bin/pip install -r backend/requirements.txt

bold "==> GitHub Copilot provider (optional)"
if backend/.venv/bin/pip install -r backend/requirements-ai.txt; then
  echo "    Copilot provider available."
else
  printf '\033[33m    Could not install the Copilot SDK. Not fatal — Fool'"'"'s Gold still\n'
  printf '    runs on Claude, OpenAI, or structural signals alone. Pick one in\n'
  printf '    Settings, or retry later with:\n'
  printf '      backend/.venv/bin/pip install -r backend/requirements-ai.txt\033[0m\n'
fi

bold "==> Electron shell + React frontend"
command -v npm >/dev/null 2>&1 || die "npm not found. Install Node 18+ (brew install node)."
npm install

cat <<MSG

$(bold "Done.")

  1. Check what's readable on this Mac:   python3 scripts/check-mail.py
  2. Launch:                              npm start
  3. In the app: pick your mail source, enter your address, refresh.

  Apple Mail setup   docs/APPLE_MAIL_SETUP.md   (no app registration needed)
  Outlook setup      docs/ENTRA_SETUP.md
MSG
