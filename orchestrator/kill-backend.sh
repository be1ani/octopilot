#!/bin/sh
# Kill the orchestrator backend (app.py) started via dev.sh.
#
# Strategy:
# - Prefer killing by TCP port (default ORCH_PORT=5050) when possible.
# - Fall back to matching the app.py command line.
set -eu

ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT="${ORCH_PORT:-5050}"
MODE="${1:-}"

if [ "$MODE" = "--help" ] || [ "$MODE" = "-h" ]; then
  echo "Usage: $0 [--force]"
  echo "Kills orchestrator backend on ORCH_PORT (default: 5050)."
  exit 0
fi

signal="TERM"
if [ "$MODE" = "--force" ]; then
  signal="KILL"
fi

killed=0

# 1) Try to kill by port (Linux: fuser is common).
if command -v fuser >/dev/null 2>&1; then
  # fuser exits non-zero if nothing is using the port; ignore that.
  if fuser -k -"$signal" "${PORT}/tcp" >/dev/null 2>&1; then
    killed=1
  fi
fi

# 2) Fall back to matching the backend command line.
if [ "$killed" -eq 0 ]; then
  app_py="$ROOT/backend/app.py"
  if command -v pgrep >/dev/null 2>&1; then
    pids="$(pgrep -f "$app_py" 2>/dev/null || true)"
    if [ -n "${pids:-}" ]; then
      # shellcheck disable=SC2086
      kill -"$signal" $pids 2>/dev/null || true
      killed=1
    fi
  fi
fi

if [ "$killed" -eq 0 ]; then
  echo "No backend process found (ORCH_PORT=$PORT)."
  exit 0
fi

echo "Backend kill signal sent ($signal)."
