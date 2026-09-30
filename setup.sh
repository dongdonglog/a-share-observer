#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COLLECT=0
START=0
AUTOSTART=0

usage() {
  cat <<'EOF'
Usage: ./setup.sh [--collect] [--start | --autostart]

  --collect    Fetch the first set of public market data (network access).
  --start      Start the local dashboard in the background.
  --autostart  Install and start the macOS LaunchAgent (includes daily
               post-close collection on trading days).
EOF
}

while (($#)); do
  case "$1" in
    --collect) COLLECT=1 ;;
    --start) START=1 ;;
    --autostart) AUTOSTART=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if ((START && AUTOSTART)); then
  echo "Choose either --start or --autostart." >&2
  exit 2
fi
if ((AUTOSTART)) && [[ "$(uname -s)" != "Darwin" ]]; then
  echo "--autostart currently requires macOS (launchd)." >&2
  echo "On this system, start the dashboard with ./.venv/bin/python run.py dash." >&2
  exit 2
fi

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  echo "Python 3.10 or newer is required. Install Python and retry." >&2
  exit 1
fi
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  echo "Python 3.10 or newer is required; found: $($PYTHON_BIN --version 2>&1)" >&2
  exit 1
fi

VENV="$ROOT/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV"
fi
APP_PYTHON="$VENV/bin/python"

cd "$ROOT"
"$APP_PYTHON" run.py status

if ((COLLECT)); then
  "$APP_PYTHON" run.py daily
fi

if ((AUTOSTART)); then
  PYTHON="$APP_PYTHON" "$ROOT/tools/dash.sh" install
elif ((START)); then
  PYTHON="$APP_PYTHON" "$ROOT/tools/dash.sh" start
else
  cat <<'EOF'

Setup complete. Start the dashboard with:
  ./.venv/bin/python run.py dash

For a first data collection and macOS login startup, use:
  ./setup.sh --collect --autostart
EOF
fi
