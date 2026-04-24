#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"

# Prefer Playwright's resolved Chromium path (matches the installed pip + browser revision).
# Fall back to scanning PLAYWRIGHT_BROWSERS_PATH and ~/.cache/ms-playwright.
_resolve_chromium() {
  python3 - <<'PY' 2>/dev/null || true
import os
try:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        path = p.chromium.executable_path
    if path and os.path.isfile(path) and os.access(path, os.X_OK):
        print(path)
except Exception:
    pass
PY
}

if [[ -z "${BROWSER_USE_BROWSER_BINARY:-}" ]]; then
  _chrome="$(_resolve_chromium)"
  if [[ -z "${_chrome}" ]]; then
    for _root in "${PLAYWRIGHT_BROWSERS_PATH}" "${HOME}/.cache/ms-playwright"; do
      [[ -z "${_root}" || ! -d "${_root}" ]] && continue
      # Full Chromium only (not headless_shell); required for headed browser-use + Xvfb.
      _chrome="$(find "${_root}" -path '*/chrome-linux/chrome' -type f 2>/dev/null | head -n1)"
      [[ -n "${_chrome}" ]] && break
    done
  fi
  if [[ -n "${_chrome}" ]]; then
    export BROWSER_USE_BROWSER_BINARY="${_chrome}"
  fi
fi

echo "[entrypoint] DISPLAY=${DISPLAY}"
if [[ -n "${BROWSER_USE_BROWSER_BINARY:-}" ]]; then
  echo "[entrypoint] BROWSER_USE_BROWSER_BINARY=${BROWSER_USE_BROWSER_BINARY}"
else
  echo "[entrypoint] WARNING: BROWSER_USE_BROWSER_BINARY not set; install Chrome/Chromium or set the env var."
fi

# Portrait / mobile-like virtual display (width x height x depth). Override with XVFB_RESOLUTION or AGENT_VIEW_*.
# Defaults: 1080x1920 — full-HD in portrait; good for noVNC and mobile-style layouts.
export AGENT_BROWSER_FULL_DISPLAY="${AGENT_BROWSER_FULL_DISPLAY:-1}"
if [[ -z "${AGENT_VIEW_WIDTH:-}" || -z "${AGENT_VIEW_HEIGHT:-}" ]]; then
  _xr="${XVFB_RESOLUTION:-1080x1920x24}"
  if [[ "${_xr}" =~ ^([0-9]+)x([0-9]+)x[0-9]+$ ]]; then
    export AGENT_VIEW_WIDTH="${AGENT_VIEW_WIDTH:-${BASH_REMATCH[1]}}"
    export AGENT_VIEW_HEIGHT="${AGENT_VIEW_HEIGHT:-${BASH_REMATCH[2]}}"
  else
    export AGENT_VIEW_WIDTH="${AGENT_VIEW_WIDTH:-1080}"
    export AGENT_VIEW_HEIGHT="${AGENT_VIEW_HEIGHT:-1920}"
  fi
fi
export XVFB_RESOLUTION="${XVFB_RESOLUTION:-${AGENT_VIEW_WIDTH}x${AGENT_VIEW_HEIGHT}x24}"

echo "[entrypoint] Xvfb / viewport: ${XVFB_RESOLUTION} (AGENT_VIEW ${AGENT_VIEW_WIDTH}x${AGENT_VIEW_HEIGHT}, full display=${AGENT_BROWSER_FULL_DISPLAY})"

# UI theming for embedded noVNC + ttyd pages.
_install_web_themes() {
  local novnc_dir="/usr/share/novnc"
  local src_css="/opt/okto-themes/okto-novnc.css"
  local dst_css="${novnc_dir}/okto-theme.css"
  local vnc_html="${novnc_dir}/vnc_lite.html"

  if [[ -d "${novnc_dir}" && -f "${src_css}" ]]; then
    cp -f "${src_css}" "${dst_css}" 2>/dev/null || true
  fi

  # Patch noVNC HTML once to load the theme CSS.
  if [[ -f "${vnc_html}" && -f "${dst_css}" ]]; then
    if ! grep -q 'okto-theme\.css' "${vnc_html}" 2>/dev/null; then
      # Insert before </head> (case-insensitive-ish).
      sed -i 's#</head>#  <link rel="stylesheet" href="okto-theme.css">\n</head>#I' "${vnc_html}" 2>/dev/null || true
    fi
  fi
}

_install_web_themes

# Virtual framebuffer + minimal WM (helps some Chromium focus/placement behavior).
Xvfb "${DISPLAY}" -screen 0 "${XVFB_RESOLUTION}" -ac +extension RANDR +extension GLX &
XVFB_PID=$!

_cleanup() {
  kill "${XVFB_PID}" 2>/dev/null || true
}
trap _cleanup EXIT

for _ in $(seq 1 50); do
  if xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

if ! xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
  echo "[entrypoint] ERROR: Xvfb failed to start on ${DISPLAY}" >&2
  exit 1
fi

if command -v fluxbox >/dev/null 2>&1; then
  fluxbox &
fi

# VNC: localhost only; noVNC/websockify is the network edge.
x11vnc -display "${DISPLAY}" -forever -shared -nopw -listen 127.0.0.1 -rfbport 5900 &
python3 -m websockify --web=/usr/share/novnc 0.0.0.0:6080 localhost:5900 &

echo "[entrypoint] GUI: noVNC http://0.0.0.0:6080/vnc_lite.html"
echo "[entrypoint] Terminal: ttyd http://0.0.0.0:7681/term/"

# Keep the terminal alive even if the agent command exits.
#
# We create a persistent tmux session, start the agent command in its own window,
# and attach ttyd to the tmux session. That way:
# - the terminal doesn't "error and exit" when the agent finishes quickly
# - you can inspect logs / rerun commands interactively in the same container
SESSION_NAME="${TTYD_TMUX_SESSION:-agent}"
TTYD_THEME_JSON="${TTYD_THEME_JSON:-{\"background\":\"#020617\",\"foreground\":\"#e2e8f0\",\"cursor\":\"#38bdf8\",\"selectionBackground\":\"rgba(56,189,248,0.25)\"}}"
TTYD_INDEX_PATH="${TTYD_INDEX_PATH:-/opt/okto-themes/ttyd-inline.html}"

# Ensure session exists (detached) with a shell window.
if ! tmux has-session -t "${SESSION_NAME}" 2>/dev/null; then
  tmux new-session -d -s "${SESSION_NAME}" -n shell
fi

# Larger scrollback so agent output is not truncated in the web terminal.
tmux set-option -t "${SESSION_NAME}" history-limit 100000 2>/dev/null || true

# ttyd must attach to the window that runs the agent. A bare `attach -t session` lands on
# the first window (`shell`), so users only saw bash unless they switched manually.
_ATTACH_TARGET="${SESSION_NAME}"

# If a command was provided (i.e. orchestrator starts the agent), run it in its own window.
if [[ $# -gt 0 ]]; then
  # Skip no-op default CMD.
  if ! [[ "$1" == "sleep" && "${2:-}" == "infinity" ]]; then
    if ! tmux list-windows -t "${SESSION_NAME}" -F '#{window_name}' 2>/dev/null | grep -qx 'agent'; then
      tmux new-window -t "${SESSION_NAME}" -n agent -- "$@"
    fi
    # Default is to destroy the pane when the agent exits; then only the empty `shell`
    # window remains and the web terminal looks "cleared". Keep the pane + scrollback.
    tmux set-window-option -t "${SESSION_NAME}:agent" remain-on-exit on 2>/dev/null || true
    _ATTACH_TARGET="${SESSION_NAME}:agent"
  fi
fi

# Persist agent pane output to a bind-mounted file on the Docker host (orchestrator reads it via API).
AGENT_TERMINAL_LOG="${AGENT_TERMINAL_LOG:-/var/log/agent-terminal.log}"
if [[ "${_ATTACH_TARGET}" == *":agent" ]]; then
  mkdir -p "$(dirname "${AGENT_TERMINAL_LOG}")" 2>/dev/null || true
  touch "${AGENT_TERMINAL_LOG}" 2>/dev/null || true
  tmux pipe-pane -t "${SESSION_NAME}:agent" -o "cat >>${AGENT_TERMINAL_LOG}"
fi

_ttyd_args=(-W -p "${TTYD_PORT:-7681}" -b /term -t "theme=${TTYD_THEME_JSON}")
if [[ -f "${TTYD_INDEX_PATH}" ]]; then
  _ttyd_args+=(-I "${TTYD_INDEX_PATH}")
fi
exec ttyd "${_ttyd_args[@]}" -- tmux attach -t "${_ATTACH_TARGET}"

