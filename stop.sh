#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

usage() {
  cat <<'EOF'
Usage:
  ./stop.sh          # stop services started by start.sh (docker compose)
  ./stop.sh --down   # also remove containers + network + volumes
EOF
}

DOWN=false
case "${1:-}" in
  "" ) ;;
  --down ) DOWN=true ;;
  -h|--help ) usage; exit 0 ;;
  * ) echo "Unknown option: ${1}" >&2; usage; exit 2 ;;
esac

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "Missing required command: docker compose (or docker-compose)" >&2
  exit 1
fi

echo "Stopping services..."
if [ "${DOWN}" = "true" ]; then
  "${COMPOSE[@]}" down -v
else
  "${COMPOSE[@]}" down
fi

echo "Done."

