#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

require_cmd docker
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker compose >/dev/null 2>&1; then
  COMPOSE=(docker compose)
else
  echo "Missing required command: docker compose (or docker compose)" >&2
  exit 1
fi

echo "Starting services with docker compose..."
"${COMPOSE[@]}" up -d --build

echo
echo "Application is up."
echo "- Job Board API:    http://localhost:5060/api/health"
echo "- Orchestrator API: http://localhost:5050/api/health"
echo "- Job Board UI:     http://localhost:5070/"
echo "- Orchestrator UI:  http://localhost:5080/"
