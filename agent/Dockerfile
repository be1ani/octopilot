# Agent image: Chromium (Playwright), virtual X11, VNC + noVNC, web terminal (ttyd).
# Kubernetes: expose 6080 (noVNC) and 7681 (ttyd), or front them with an Ingress later.
#
# Use noble (Ubuntu 24.04): Python 3.12. The jammy image ships Python 3.10, which cannot
# install browser-use>=0.12.6 (requires Python >=3.11).

FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DISPLAY=:99 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    AGENT_BROWSER_FULL_DISPLAY=1 \
    AGENT_VIEW_WIDTH=1080 \
    AGENT_VIEW_HEIGHT=1920

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    fluxbox \
    novnc \
    tmux \
    x11vnc \
    x11-utils \
    xvfb \
  && rm -rf /var/lib/apt/lists/*

ARG TTYD_VERSION=1.7.7
RUN set -eux; \
  arch="$(dpkg --print-architecture)"; \
  case "${arch}" in \
    amd64) ttyd_arch=x86_64 ;; \
    arm64) ttyd_arch=aarch64 ;; \
    *) echo "unsupported arch: ${arch}" >&2; exit 1 ;; \
  esac; \
  curl -fsSL -o /usr/local/bin/ttyd \
    "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${ttyd_arch}"; \
  chmod +x /usr/local/bin/ttyd

# Build ttyd's UI `inline.html` so we can inject custom CSS (scrollbars, dark page bg).
# We keep using the prebuilt ttyd server binary above.
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    git \
    nodejs \
    npm \
  && rm -rf /var/lib/apt/lists/*

# ttyd >=1.7 dropped the html/Makefile; the UI is built with Yarn 3 + webpack + gulp (`yarn run inline` -> dist/inline.html).
RUN set -eux; \
  git clone --depth 1 --branch "${TTYD_VERSION}" https://github.com/tsl0922/ttyd /tmp/ttyd-src; \
  cd /tmp/ttyd-src/html; \
  npm install -g corepack; \
  corepack enable; \
  yarn install; \
  yarn run inline; \
  mkdir -p /opt/okto-themes; \
  cp -f dist/inline.html /opt/okto-themes/ttyd-inline.html; \
  cd / && rm -rf /tmp/ttyd-src

COPY agent/requirements.txt /app/agent/requirements.txt
# Noble image may ship typing_extensions (and similar) via apt without a pip RECORD;
# pip then fails with "uninstall-no-record-file". Install PyPI wheels without uninstalling those.
RUN pip install --upgrade pip \
  && pip install --ignore-installed -r /app/agent/requirements.txt \
  && pip install websockify \
  && playwright install chromium

COPY agent/ /app/agent/
COPY profiles/ /app/profiles/
COPY master_profile_schema.json /app/master_profile_schema.json

COPY agent/docker/themes/ /opt/okto-themes/

# Inject our CSS into the built ttyd inline.html.
RUN python3 - <<'PY'
from pathlib import Path

css_path = Path("/opt/okto-themes/okto-ttyd.css")
html_path = Path("/opt/okto-themes/ttyd-inline.html")

if css_path.exists() and html_path.exists():
    css = css_path.read_text(encoding="utf-8")
    html = html_path.read_text(encoding="utf-8")
    needle = "</head>"
    if needle in html and "<style>" not in html:
        html = html.replace(needle, f"<style>\n{css}\n</style>\n{needle}", 1)
        html_path.write_text(html, encoding="utf-8")
PY

COPY agent/docker/entrypoint.sh /usr/local/bin/agent-entrypoint.sh
RUN chmod +x /usr/local/bin/agent-entrypoint.sh

EXPOSE 6080 7681

ENTRYPOINT ["/usr/local/bin/agent-entrypoint.sh"]
CMD ["sleep", "infinity"]

