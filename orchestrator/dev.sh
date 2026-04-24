#!/bin/sh
# POSIX `sh` (e.g. dash): do not use bash-only `set -o pipefail`, so `sh dev.sh` works.
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
export ORCH_PORT="${ORCH_PORT:-5050}"
(
  cd "$ROOT/backend"
  if [ ! -f "backenv/bin/activate" ]; then
    echo "backend venv not found: $ROOT/backend/backenv" >&2
    echo "create it first (example): python3 -m venv backenv && . backenv/bin/activate && pip install -r requirements.txt" >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  . "backenv/bin/activate"
  exec "${PYTHON:-python3}" app.py
) &
BACK_PID=$!
trap 'kill "$BACK_PID" 2>/dev/null || true' EXIT INT TERM
cd "$ROOT/frontend"
exec npm run dev
