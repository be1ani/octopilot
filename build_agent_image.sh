#!/bin/sh
set -eu

# Builds the agent image from agent/Dockerfile and bumps the version in agent/VERSION.
# Tags:
#   octopilot-agent:<version>   (e.g. v0.2)
#   octopilot-agent:latest

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
VERSION_FILE="${ROOT_DIR}/agent/VERSION"
IMAGE_REPO="${IMAGE_REPO:-octopilot-agent}"

_read_version() {
  if [ -f "${VERSION_FILE}" ]; then
    tr -d ' \t\r\n' < "${VERSION_FILE}"
    return 0
  fi
  echo "v0.1"
}

_bump_version() {
  v="$1"
  major="$(printf "%s" "${v}" | sed -n 's/^v\([0-9][0-9]*\)\.\([0-9][0-9]*\)$/\1/p')"
  minor="$(printf "%s" "${v}" | sed -n 's/^v\([0-9][0-9]*\)\.\([0-9][0-9]*\)$/\2/p')"
  if [ -z "${major}" ] || [ -z "${minor}" ]; then
    echo "Invalid version in ${VERSION_FILE}: ${v}" >&2
    echo "Expected format: v<major>.<minor>  (e.g. v0.1)" >&2
    exit 2
  fi
  minor_next=$((minor + 1))
  echo "v${major}.${minor_next}"
}

current="$(_read_version)"
next="$(_bump_version "${current}")"

echo "[build] docker build -f agent/Dockerfile -t ${IMAGE_REPO}:${next} -t ${IMAGE_REPO}:latest ."
echo "[build] next tag will be ${next} (version file is still ${current} until the build succeeds)"
docker build -f agent/Dockerfile -t "${IMAGE_REPO}:${next}" -t "${IMAGE_REPO}:latest" "${ROOT_DIR}"

echo "${next}" > "${VERSION_FILE}"
echo "[build] bumped ${current} -> ${next} in ${VERSION_FILE}"
echo "[build] built ${IMAGE_REPO}:${next} (also tagged latest)"

