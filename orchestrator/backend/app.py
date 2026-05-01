#!/usr/bin/env python3
"""
Orchestrator API: manage octopilot-agent Docker containers (noVNC + ttyd) from a job URL.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, request, send_from_directory
from flask_cors import CORS
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import PyMongoError

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = REPO_ROOT / "orchestrator" / "frontend" / "dist"
STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "state.json"
APPLICATION_LOG_PATH = Path(__file__).resolve().parents[1] / "data" / "applications.jsonl"
SCREENSHOTS_DIR = Path(__file__).resolve().parents[1] / "data" / "screenshots"
# Max allowed decoded screenshot size (8 MB). Larger payloads are rejected.
SCREENSHOT_MAX_BYTES = int(os.environ.get("ORCH_SCREENSHOT_MAX_BYTES", str(8 * 1024 * 1024)))
# Hard cap on stored screenshots per run to avoid runaway disk usage.
SCREENSHOT_MAX_PER_RUN = int(os.environ.get("ORCH_SCREENSHOT_MAX_PER_RUN", "200"))
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Captured tmux agent-pane output per machine (bind-mounted from agent containers).
TERMINAL_LOG_MAX_BYTES = int(os.environ.get("ORCH_TERMINAL_LOG_MAX_BYTES", str(8 * 1024 * 1024)))

DEFAULT_IMAGE = os.environ.get("ORCH_DOCKER_IMAGE", "octopilot-agent:latest")
DEFAULT_PROFILE = os.environ.get("ORCH_DEFAULT_PROFILE_ID", "main")
DEFAULT_OPENAI_MODEL = (os.environ.get("ORCH_DEFAULT_OPENAI_MODEL") or "gpt-5.4").strip()
PUBLIC_HOST = os.environ.get("ORCH_PUBLIC_HOST", "127.0.0.1")
COST_PER_HOUR_USD = float(os.environ.get("ORCH_COST_USD_PER_HOUR", "0.05"))
HOST_REPO_ROOT = (os.environ.get("ORCH_HOST_REPO_ROOT") or "").strip() or None
DEFAULT_LLM_LEDGER_REL = os.environ.get("ORCH_LLM_LEDGER_REL", "orchestrator/data/llm_ledger.jsonl").strip()

AGENT_VIEW_W = int(os.environ.get("ORCH_AGENT_VIEW_WIDTH", "568"))
AGENT_VIEW_H = int(os.environ.get("ORCH_AGENT_VIEW_HEIGHT", "800"))


@dataclass
class Machine:
    id: str
    job_url: str
    profile_id: str
    llm_model: str | None
    image: str
    status: str  # starting | running | stopped | error
    error: str | None = None
    container_id: str | None = None
    desktop_port: int | None = None
    terminal_port: int | None = None
    started_at: float | None = None
    stopped_at: float | None = None
    session_cost_usd: float | None = None
    applications_submitted: int = 0
    llm_tokens: int | None = None
    llm_cost_usd: float | None = None
    needs_human: bool = False
    needs_human_reason: str | None = None
    needs_human_at: float | None = None
    created_at: float = field(default_factory=lambda: time.time())
    agent_paused: bool = False
    # Cooperative control (file-based). "running" | "paused" | "stopping".
    # Reported by the agent itself via telemetry; unset while the agent hasn't
    # reported yet.
    agent_state: str | None = None
    agent_state_at: float | None = None
    # Optional job metadata (from job board / API) for placeholders and human review.
    job_title: str | None = None
    job_company: str | None = None
    job_city: str | None = None


def _state_path() -> Path:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    return STATE_PATH


def _applications_path() -> Path:
    APPLICATION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    return APPLICATION_LOG_PATH


def _utc_iso(ts: float | None = None) -> str:
    t = float(ts if ts is not None else time.time())
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


def _terminal_log_rel(mid: str) -> str:
    return f"orchestrator/data/terminal_logs/{mid}.log"


# ---------------------------------------------------------------------------
# Cooperative agent control (file-based pause / takeover)
# ---------------------------------------------------------------------------
#
# The orchestrator cannot reliably SIGSTOP the agent process in every setup
# (pgrep may miss the PID, docker exec may fail, signals don't stop in-flight
# HTTP calls, etc). Instead we write a tiny JSON control file into a directory
# bind-mounted into the agent container. The agent checks that file before
# every LLM call and blocks / exits accordingly. This is much more reliable
# and, unlike SIGSTOP, does not freeze the VNC/terminal server processes so
# the user can still take the desktop over.

CONTROL_DIR_REL_TMPL = "orchestrator/data/control/{mid}"
CONTROL_FILE_NAME = "state.json"
USER_GUIDANCE_FILE = "user_guidance.txt"
CONTROL_MOUNT_PATH = "/var/run/okto-control"


def _control_dir_rel(mid: str) -> str:
    return CONTROL_DIR_REL_TMPL.format(mid=mid)


def _prepare_control_bind(mid: str, *, initial_state: str = "running") -> tuple[str, str]:
    """
    Ensure the host-side control directory exists, seed it with an initial
    state file, and return (host_path, container_path) for docker -v.
    """
    check, host_path = _mountable_paths(_control_dir_rel(mid))
    check.mkdir(parents=True, exist_ok=True)
    state_path = check / CONTROL_FILE_NAME
    try:
        state_path.write_text(
            json.dumps({"state": initial_state, "ts": time.time()}),
            encoding="utf-8",
        )
    except OSError:
        pass
    return str(host_path), CONTROL_MOUNT_PATH


def _write_control_state(mid: str, *, state: str) -> None:
    """
    Write the requested control state ("running" | "paused" | "stopping").
    Safe to call even when the container isn't running — the agent will pick
    it up the next time it starts.
    """
    check, _host = _mountable_paths(_control_dir_rel(mid))
    check.mkdir(parents=True, exist_ok=True)
    state_path = check / CONTROL_FILE_NAME
    tmp = state_path.with_suffix(".json.tmp")
    payload = {"state": state, "ts": time.time()}
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(state_path)


def _append_terminal_session_banner(mid: str, event: str) -> None:
    """Append a session marker so restarts accumulate in one file per machine."""
    check, _ = _mountable_paths(_terminal_log_rel(mid))
    check.parent.mkdir(parents=True, exist_ok=True)
    sep = "=" * 72
    banner = f"\n{sep}\n{_utc_iso()}  [{event}]  machine_id={mid}\n{sep}\n"
    with check.open("a", encoding="utf-8") as f:
        f.write(banner)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass


def _prepare_terminal_log_bind(mid: str, event: str) -> tuple[str, str]:
    """
    Ensure the host log file exists and return (host_path, container_path) for docker -v.
    """
    _append_terminal_session_banner(mid, event)
    check, host_path = _mountable_paths(_terminal_log_rel(mid))
    check.parent.mkdir(parents=True, exist_ok=True)
    check.touch(exist_ok=True)
    return str(host_path), "/var/log/agent-terminal.log"


def _read_terminal_log(mid: str) -> tuple[str, bool, int]:
    """Return (text, truncated, total_bytes_on_disk)."""
    p, _ = _mountable_paths(_terminal_log_rel(mid))
    if not p.is_file():
        return "", False, 0
    sz = p.stat().st_size
    max_b = max(4096, TERMINAL_LOG_MAX_BYTES)
    with p.open("rb") as f:
        if sz <= max_b:
            raw = f.read()
            truncated = False
        else:
            f.seek(sz - max_b)
            raw = f.read()
            truncated = True
    text = raw.decode("utf-8", errors="replace")
    return text, truncated, sz


def append_application_record(record: dict[str, Any]) -> None:
    """
    Persist a single application record.
    """
    try:
        coll = _applications_coll()
        doc = dict(record or {})
        # Ensure id exists so updates can target it.
        if "id" not in doc or not str(doc.get("id") or "").strip():
            doc["id"] = str(uuid.uuid4())
        # Normalize ts for sorting.
        if "ts" not in doc and isinstance(doc.get("created_at"), (int, float)):
            doc["ts"] = float(doc["created_at"])
        if "ts" not in doc:
            doc["ts"] = time.time()
        coll.update_one({"id": doc["id"]}, {"$set": doc}, upsert=True)
    except PyMongoError:
        # Best-effort: app records should never crash the main loop.
        return


def read_application_records(limit: int = 500) -> list[dict[str, Any]]:
    try:
        coll = _applications_coll()
        cur = coll.find({}, {"_id": 0}).sort([("ts", -1)]).limit(max(1, int(limit or 500)))
        return [doc for doc in cur if isinstance(doc, dict)]
    except PyMongoError:
        return []


_applications_lock = threading.RLock()

# Legacy profiles_db.json helpers are kept only for one-time import into Mongo.
_profiles_lock = threading.RLock()
_mongo_lock = threading.RLock()
_mongo_client: MongoClient | None = None


def _orch_state_collection_name() -> str:
    return (os.environ.get("ORCH_STATE_COLLECTION") or "").strip() or "orch_state"


def _orch_applications_collection_name() -> str:
    return (os.environ.get("ORCH_APPLICATIONS_COLLECTION") or "").strip() or "applications"


def _orch_llm_ledger_collection_name() -> str:
    return (os.environ.get("ORCH_LLM_LEDGER_COLLECTION") or "").strip() or "llm_ledger"


def _orch_human_input_collection_name() -> str:
    return (os.environ.get("ORCH_HUMAN_INPUT_COLLECTION") or "").strip() or "human_input_requests"


def _human_input_poll_hint_s(created_at: float) -> float:
    """Match agent-side backoff (caps at 5s) for UI display."""
    e = max(0.0, time.time() - float(created_at))
    if e < 15.0:
        return 0.25
    if e < 30.0:
        return 0.5
    if e < 60.0:
        return 1.0
    if e < 120.0:
        return 2.0
    return 5.0


def _human_input_coll() -> Collection:
    db = _mongo()[_orch_mongo_db_name()]
    coll = db[_orch_human_input_collection_name()]
    try:
        coll.create_index([("machine_id", 1), ("status", 1)], name="machine_status")
        coll.create_index([("created_at", -1)], name="created_desc")
    except PyMongoError:
        pass
    return coll


def _state_coll() -> Collection:
    db = _mongo()[_orch_mongo_db_name()]
    return db[_orch_state_collection_name()]


def _applications_coll() -> Collection:
    db = _mongo()[_orch_mongo_db_name()]
    coll = db[_orch_applications_collection_name()]
    try:
        coll.create_index([("id", 1)], unique=True, name="app_id_unique")
        coll.create_index([("ts", -1)], name="ts_desc")
        coll.create_index([("machine_id", 1), ("ts", -1)], name="machine_ts")
    except PyMongoError:
        pass
    return coll


def _llm_ledger_coll() -> Collection:
    db = _mongo()[_orch_mongo_db_name()]
    coll = db[_orch_llm_ledger_collection_name()]
    try:
        coll.create_index([("ts", -1)], name="ts_desc")
        coll.create_index([("machine_id", 1), ("ts", -1)], name="machine_ts")
        coll.create_index([("model", 1), ("ts", -1)], name="model_ts")
    except PyMongoError:
        pass
    return coll


def _orch_mongo_uri() -> str:
    return (
        (os.environ.get("ORCH_MONGO_URI") or "").strip()
        or "mongodb://127.0.0.1:27017"
    )


def _orch_mongo_db_name() -> str:
    return (os.environ.get("ORCH_MONGO_DB_NAME") or "").strip() or "orchestrator_db"


def _orch_profiles_collection_name() -> str:
    return (os.environ.get("ORCH_PROFILES_COLLECTION") or "").strip() or "profiles"


def _orch_llm_providers_collection_name() -> str:
    return (os.environ.get("ORCH_LLM_PROVIDERS_COLLECTION") or "").strip() or "llm_providers"


# Catalog of LLM providers the UI knows how to add. The order here is the
# order the dropdown shows; `env_var` is what gets injected into agent
# containers as -e flags so the per-job code (browser-use, langchain, etc.)
# can pick the credential up. `model_prefixes` is used to bucket entries from
# agent/pricing.json by provider so the table can show their per-model cost.
# `agent_provider` is the value the agent process expects in
# `AGENT_LLM_PROVIDER` (see `_make_agent_llm` in agent/cli.py).
# `model_env_var` is the per-provider env var the agent reads to pick the
# specific model id (e.g. OPENAI_MODEL, DEEPSEEK_MODEL, …).
LLM_PROVIDER_REGISTRY: list[dict[str, Any]] = [
    {
        "id": "openai",
        "label": "OpenAI",
        "env_var": "OPENAI_API_KEY",
        "model_prefixes": ("gpt-", "o1", "o3", "o4", "chatgpt-", "text-embedding-", "computer-use-"),
        "agent_provider": "openai",
        "model_env_var": "OPENAI_MODEL",
    },
    {
        "id": "openai-admin",
        "label": "OpenAI Admin (usage reconciliation)",
        "env_var": "OPENAI_ADMIN_KEY",
        "model_prefixes": (),
        # Reconciliation-only credential: never selectable as an agent
        # provider.
        "agent_provider": None,
        "model_env_var": None,
    },
    {
        "id": "anthropic",
        "label": "Claude (Anthropic)",
        "env_var": "ANTHROPIC_API_KEY",
        "model_prefixes": ("claude",),
        "agent_provider": "anthropic",
        "model_env_var": "ANTHROPIC_MODEL",
    },
    {
        "id": "google",
        "label": "Google (Gemini)",
        "env_var": "GOOGLE_API_KEY",
        "model_prefixes": ("gemini",),
        "agent_provider": "google",
        "model_env_var": "GOOGLE_MODEL",
    },
    {
        "id": "deepseek",
        "label": "DeepSeek",
        "env_var": "DEEPSEEK_API_KEY",
        "model_prefixes": ("deepseek-",),
        "agent_provider": "deepseek",
        "model_env_var": "DEEPSEEK_MODEL",
    },
    {
        "id": "browser-use",
        "label": "Browser-Use",
        "env_var": "BROWSER_USE_API_KEY",
        "model_prefixes": ("bu-",),
        "agent_provider": "browser_use",
        "model_env_var": "BROWSER_USE_MODEL",
    },
]

LLM_PROVIDER_INDEX: dict[str, dict[str, Any]] = {p["id"]: p for p in LLM_PROVIDER_REGISTRY}


def _llm_providers_coll() -> Collection:
    db = _mongo()[_orch_mongo_db_name()]
    coll = db[_orch_llm_providers_collection_name()]
    try:
        coll.create_index([("provider", 1)], unique=True, name="provider_unique")
    except PyMongoError:
        pass
    return coll


def _mask_api_key(key: str) -> str:
    s = (key or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return "•" * len(s)
    return f"{s[:4]}…{s[-4:]}"


def _provider_for_model(model_id: str) -> str | None:
    """Match a pricing.json model id to a registry provider id."""
    s = (model_id or "").strip().lower()
    if not s:
        return None
    for p in LLM_PROVIDER_REGISTRY:
        for prefix in p.get("model_prefixes") or ():
            if s.startswith(prefix):
                return p["id"]
    return None


def _load_pricing_json() -> dict[str, Any]:
    """Read agent/pricing.json (host-side) for read-only display."""
    p = REPO_ROOT / "agent" / "pricing.json"
    if not p.is_file():
        return {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _models_for_provider(provider_id: str) -> list[dict[str, Any]]:
    """Return [{id, usd_per_1m_input, usd_per_1m_output, ...}] for one provider."""
    pricing = _load_pricing_json()
    models = pricing.get("models")
    if not isinstance(models, dict):
        return []
    rows: list[dict[str, Any]] = []
    for model_id, info in models.items():
        if not isinstance(info, dict):
            continue
        if _provider_for_model(model_id) != provider_id:
            continue
        row = {"id": model_id}
        for k in ("usd_per_1m_input", "usd_per_1m_output", "usd_per_1m_cached_input"):
            v = info.get(k)
            if isinstance(v, (int, float)):
                row[k] = float(v)
        rows.append(row)
    rows.sort(key=lambda r: r["id"])
    return rows


def _ledger_spend_by_provider(*, ledger_rel: str, max_lines: int = 20000) -> dict[str, float]:
    """Sum cost_usd from the JSONL ledger, grouped by `provider` field."""
    ledger_path, _ = _mountable_paths(ledger_rel)
    if not ledger_path.is_file():
        return {}
    totals: dict[str, float] = {}
    for line in _tail_lines(ledger_path, max_lines=max_lines, max_bytes=8_000_000):
        s = (line or "").strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        prov = str(obj.get("provider") or "").strip()
        cost = obj.get("cost_usd")
        if not prov or not isinstance(cost, (int, float)):
            continue
        totals[prov] = totals.get(prov, 0.0) + float(cost)
    return totals


def _serialize_llm_provider(
    doc: dict[str, Any],
    *,
    spend_index: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build the public (key-redacted) view of a stored provider doc."""
    pid = str(doc.get("provider") or doc.get("_id") or "").strip()
    meta = LLM_PROVIDER_INDEX.get(pid) or {"id": pid, "label": pid, "env_var": "", "model_prefixes": ()}
    api_key = str(doc.get("api_key") or "")
    spend = float((spend_index or {}).get(pid, 0.0)) if spend_index else 0.0
    return {
        "provider": pid,
        "label": meta.get("label") or pid,
        "env_var": meta.get("env_var") or "",
        "key_set": bool(api_key),
        "key_masked": _mask_api_key(api_key),
        "key_last4": api_key[-4:] if len(api_key) >= 4 else "",
        "key_length": len(api_key),
        "models": _models_for_provider(pid),
        "spend_usd": round(spend, 6),
        "updated_at": doc.get("updated_at"),
        "created_at": doc.get("created_at"),
    }


def _list_llm_providers_public(spend_index: dict[str, float] | None = None) -> list[dict[str, Any]]:
    coll = _llm_providers_coll()
    docs = list(coll.find({}, {"provider": 1, "api_key": 1, "created_at": 1, "updated_at": 1}))
    if spend_index is None:
        with _state_lock:
            data = load_state()
            settings = _get_settings(data)
            ledger_rel = (settings.get("llm_ledger_relpath") or DEFAULT_LLM_LEDGER_REL).strip()
        spend_index = _ledger_spend_by_provider(ledger_rel=ledger_rel)
    return [_serialize_llm_provider(d, spend_index=spend_index) for d in docs]


def _get_llm_provider_key(provider_id: str) -> str:
    """Look up the plaintext API key for a configured provider, or '' if absent."""
    pid = (provider_id or "").strip().lower()
    if not pid:
        return ""
    try:
        coll = _llm_providers_coll()
        doc = coll.find_one({"provider": pid}, {"api_key": 1})
    except PyMongoError:
        return ""
    if not doc:
        return ""
    return str(doc.get("api_key") or "").strip()


def _llm_provider_env_args() -> list[str]:
    """
    Build the -e <NAME>=<VALUE> argument list for `docker run`, sourced from
    every provider configured in Mongo. Returns an empty list when no keys
    are stored (agents will then fail loudly when they need a key, which is
    the intended UX after the .env mount was removed).
    """
    args: list[str] = []
    try:
        coll = _llm_providers_coll()
        docs = list(coll.find({}, {"provider": 1, "api_key": 1}))
    except PyMongoError:
        return args
    for d in docs:
        pid = str(d.get("provider") or "").strip()
        meta = LLM_PROVIDER_INDEX.get(pid)
        if not meta or not meta.get("env_var"):
            continue
        key = str(d.get("api_key") or "").strip()
        if not key:
            continue
        args.extend(["-e", f"{meta['env_var']}={key}"])
    return args


def _agent_runtime_env_for_model(llm_model: str) -> list[str]:
    """
    Build the `-e AGENT_LLM_PROVIDER=...` and `-e <PROVIDER>_MODEL=...` flags
    for an agent container based on the user-selected ``llm_model``.

    Detection is prefix-based via :func:`_provider_for_model`. Anything that
    can't be mapped (custom fine-tunes, brand-new model ids) falls back to
    OpenAI so existing flows keep working as they did before this helper was
    introduced.
    """
    pid = _provider_for_model(llm_model) or "openai"
    meta = LLM_PROVIDER_INDEX.get(pid) or LLM_PROVIDER_INDEX["openai"]
    agent_provider = meta.get("agent_provider") or "openai"
    model_env_var = meta.get("model_env_var") or "OPENAI_MODEL"
    return [
        "-e",
        f"AGENT_LLM_PROVIDER={agent_provider}",
        "-e",
        f"{model_env_var}={llm_model}",
    ]


def _mongo() -> MongoClient:
    global _mongo_client
    with _mongo_lock:
        if _mongo_client is None:
            _mongo_client = MongoClient(_orch_mongo_uri(), serverSelectionTimeoutMS=4000)
        return _mongo_client


def _profiles_coll() -> Collection:
    db = _mongo()[_orch_mongo_db_name()]
    coll = db[_orch_profiles_collection_name()]
    try:
        # Ensure uniqueness per profile_id.
        coll.create_index([("profile_id", 1)], unique=True, name="profile_id_unique")
    except PyMongoError:
        # Best-effort; don't fail requests if indexes can't be created.
        pass
    return coll


def _profiles_db_path() -> Path:
    return REPO_ROOT / "profiles_db.json"


def _load_profiles_db() -> dict[str, Any]:
    p = _profiles_db_path()
    if not p.is_file():
        raise FileNotFoundError(f"Missing profiles DB at {p}")
    raw = p.read_text(encoding="utf-8")
    obj = json.loads(raw) if raw else {}
    if not isinstance(obj, dict):
        raise ValueError("profiles_db.json must be a JSON object")
    return obj


def _save_profiles_db(db: dict[str, Any]) -> None:
    p = _profiles_db_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Profile attachments helpers
#
# Layout: every uploaded file lives at  attachments/<profile_id>/<filename>
# (sanitized filename, with " (2)", " (3)" … suffixes appended on collision so
# we never silently overwrite a file). Inside the profile JSON we keep a
# `attachments` map of `display_name -> repo-relative path`. The agent already
# bind-mounts `attachments/` read-only and exposes every file under it as an
# available file at runtime.
# ---------------------------------------------------------------------------

# Repo-relative dir used for both on-disk storage and the path stored in profiles.
_ATTACHMENTS_REPO_DIR = "attachments"


def _attachments_root_local() -> Path:
    """Filesystem path where the orchestrator reads/writes attachment files."""
    root = REPO_ROOT / _ATTACHMENTS_REPO_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _profile_attachments_dir(profile_id: str) -> Path:
    """Per-profile attachments dir on disk (creates it on first use)."""
    safe_id = _safe_path_segment(profile_id)
    if not safe_id:
        raise ValueError("profile_id must contain at least one path-safe character")
    d = _attachments_root_local() / safe_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_path_segment(s: str) -> str:
    """
    Sanitize a single path segment (profile_id, filename, ...). Allows letters,
    digits, dot, underscore, dash, and space; collapses runs of whitespace.
    """
    s = (s or "").strip()
    if not s:
        return ""
    s = re.sub(r"[\\/]+", "_", s)
    s = re.sub(r"[\x00-\x1f]+", "", s)
    s = re.sub(r"[^A-Za-z0-9._\- ()]+", "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.lstrip(".")
    return s or "_"


def _ensure_attachments_map(profile: dict[str, Any]) -> dict[str, Any]:
    """Ensure profile['attachments'] exists as a dict; return it."""
    att = profile.get("attachments")
    if not isinstance(att, dict):
        att = {}
        profile["attachments"] = att
    return att


def _unique_display_name(existing: dict[str, Any], desired: str) -> str:
    """Append ' (2)', ' (3)', … until the name is free in `existing`."""
    base = desired.strip() or "Attachment"
    if base not in existing:
        return base
    n = 2
    while True:
        candidate = f"{base} ({n})"
        if candidate not in existing:
            return candidate
        n += 1


def _unique_disk_filename(directory: Path, desired: str) -> str:
    """Append ' (2)', ' (3)', … before the extension until the file is free."""
    safe = _safe_path_segment(desired) or "file"
    if not (directory / safe).exists():
        return safe
    stem, dot, ext = safe.partition(".")
    if not dot:
        stem, ext = safe, ""
    n = 2
    while True:
        candidate = f"{stem} ({n})" + (f".{ext}" if ext else "")
        if not (directory / candidate).exists():
            return candidate
        n += 1


def _attachment_record(profile_id: str, name: str, repo_rel_path: str) -> dict[str, Any]:
    """Build the JSON shape returned to the frontend for one attachment row."""
    fs_path = REPO_ROOT / repo_rel_path
    try:
        st = fs_path.stat()
        size = int(st.st_size)
        mtime = float(st.st_mtime)
    except OSError:
        size = 0
        mtime = 0.0
    import mimetypes  # local import: rarely used elsewhere
    mime, _ = mimetypes.guess_type(fs_path.name)
    iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat() if mtime else None
    return {
        "name": name,
        "filename": fs_path.name,
        "path": repo_rel_path,
        "size": size,
        "mime": mime,
        "uploaded_at": iso,
        "exists": fs_path.is_file(),
    }


def _persist_profile(coll: Collection, profile_id: str, profile: dict[str, Any]) -> None:
    """Replace the stored profile document and bump updated_at."""
    profile["profile_id"] = profile_id
    profile["updated_at"] = datetime.now(timezone.utc).isoformat()
    coll.replace_one(
        {"profile_id": profile_id},
        {"profile_id": profile_id, "profile": profile},
        upsert=True,
    )


def _ensure_custom_maps(profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Ensure profile.other.custom.relative_fields / absolute_fields exist as dicts.
    Returns (relative_fields, absolute_fields) dicts.
    """
    other = profile.get("other")
    if not isinstance(other, dict):
        other = {}
        profile["other"] = other
    custom = other.get("custom")
    if not isinstance(custom, dict):
        custom = {}
        other["custom"] = custom
    rel = custom.get("relative_fields")
    if not isinstance(rel, dict):
        rel = {}
        custom["relative_fields"] = rel
    absf = custom.get("absolute_fields")
    if not isinstance(absf, dict):
        absf = {}
        custom["absolute_fields"] = absf
    return rel, absf


def _maybe_import_profiles_json_to_mongo() -> None:
    """
    One-time import for existing repos: if the profiles collection is empty and
    profiles_db.json exists, import all applicant/profile documents.
    """
    enabled = (os.environ.get("ORCH_IMPORT_PROFILES_JSON") or "1").strip().lower() not in {"0", "false", "no"}
    if not enabled:
        return
    p = _profiles_db_path()
    if not p.is_file():
        return
    coll = _profiles_coll()
    try:
        if coll.estimated_document_count() > 0:
            return
    except PyMongoError:
        return
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(raw, dict):
        return
    docs: list[dict[str, Any]] = []
    # Back-compat: older JSON shape nested profiles under a top-level grouping key.
    old_like = any(isinstance(v, dict) and isinstance(v.get("profiles"), dict) for v in raw.values())
    if old_like:
        for _applicant_id, row in raw.items():
            if not isinstance(row, dict):
                continue
            profiles = row.get("profiles")
            if not isinstance(profiles, dict):
                continue
            for profile_id, prof in profiles.items():
                if not isinstance(profile_id, str) or not isinstance(prof, dict):
                    continue
                docs.append({"profile_id": profile_id, "profile": prof})
    else:
        for profile_id, prof in raw.items():
            if not isinstance(profile_id, str) or not isinstance(prof, dict):
                continue
            docs.append({"profile_id": profile_id, "profile": prof})
    if not docs:
        return
    try:
        coll.insert_many(docs, ordered=False)
    except PyMongoError:
        # Ignore duplicates/partial failures; this is best-effort.
        pass


def update_application_record(
    app_id: str, patch: dict[str, Any]
) -> dict[str, Any] | None:
    """
    Apply a shallow patch to the first matching application record (by id) and
    persist it in Mongo. Returns the merged record, or None if not found.
    Unknown / protected keys are dropped by the caller.
    """
    try:
        coll = _applications_coll()
        doc = coll.find_one({"id": app_id}, {"_id": 0})
        if not isinstance(doc, dict):
            return None
        merged = dict(doc)
        merged.update(patch or {})
        coll.update_one({"id": app_id}, {"$set": patch or {}}, upsert=False)
        return merged
    except PyMongoError:
        return None


def load_state() -> dict[str, Any]:
    try:
        coll = _state_coll()
        doc = coll.find_one({"_id": "orchestrator_state"}, {"_id": 0})
        if not isinstance(doc, dict):
            return {"machines": [], "total_applications_submitted": 0, "ledger_overrides": {}}
        if "ledger_overrides" not in doc:
            doc["ledger_overrides"] = {}
        return doc
    except PyMongoError:
        return {"machines": [], "total_applications_submitted": 0, "ledger_overrides": {}}


def save_state(data: dict[str, Any]) -> None:
    try:
        coll = _state_coll()
        coll.update_one({"_id": "orchestrator_state"}, {"$set": dict(data or {})}, upsert=True)
    except PyMongoError:
        return


def _tail_lines(path: Path, *, max_lines: int = 200, max_bytes: int = 2_000_000) -> list[str]:
    """
    Read up to `max_lines` lines from the end of a text file without loading it all.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return []
    # Read at most max_bytes from the end.
    to_read = min(int(max_bytes), int(size))
    try:
        with path.open("rb") as f:
            f.seek(size - to_read)
            buf = f.read(to_read)
    except OSError:
        return []
    try:
        text = buf.decode("utf-8", errors="replace")
    except Exception:
        return []
    lines = text.splitlines()
    # If we started mid-file, the first line may be partial; drop it.
    if size > to_read and lines:
        lines = lines[1:]
    return lines[-max_lines:]


def _valid_openai_model_id(s: str) -> bool:
    """
    Cheap allow-list check for an LLM model id.

    The name dates from when only OpenAI models were supported; the regex is
    intentionally permissive enough to cover every provider we now plug in
    (e.g. `deepseek-chat`, `claude-opus-4-6`, `gemini-flash-latest`, `bu-2-0`).
    """
    s = (s or "").strip()
    if not s or len(s) > 96:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._-]+", s))


def _machine_from_dict(d: dict[str, Any]) -> Machine:
    return Machine(
        id=d["id"],
        job_url=d["job_url"],
        profile_id=d.get("profile_id") or DEFAULT_PROFILE,
        llm_model=(d.get("llm_model") or "").strip() or None,
        image=(d.get("image") or DEFAULT_IMAGE),
        status=d.get("status") or "stopped",
        error=d.get("error"),
        container_id=d.get("container_id"),
        desktop_port=d.get("desktop_port"),
        terminal_port=d.get("terminal_port"),
        started_at=d.get("started_at"),
        stopped_at=d.get("stopped_at"),
        session_cost_usd=d.get("session_cost_usd"),
        applications_submitted=int(d.get("applications_submitted") or 0),
        llm_tokens=d.get("llm_tokens"),
        llm_cost_usd=d.get("llm_cost_usd"),
        needs_human=bool(d.get("needs_human") or False),
        needs_human_reason=d.get("needs_human_reason"),
        needs_human_at=d.get("needs_human_at"),
        created_at=float(d.get("created_at") or time.time()),
        agent_paused=bool(d.get("agent_paused") or False),
        agent_state=(d.get("agent_state") or None),
        agent_state_at=d.get("agent_state_at"),
        job_title=(d.get("job_title") or "").strip() or None,
        job_company=(d.get("job_company") or "").strip() or None,
        job_city=(d.get("job_city") or "").strip() or None,
    )


def _docker_exe() -> str | None:
    """
    Resolve the docker CLI. GUI/IDE-launched Python often has a minimal PATH, so `docker`
    may be missing even when it works in a normal terminal. Set ORCH_DOCKER_BIN=/usr/bin/docker
    (or your `which docker` path) if needed.
    """
    for key in ("ORCH_DOCKER_BIN", "DOCKER_BIN"):
        raw = (os.environ.get(key) or "").strip()
        if not raw:
            continue
        p = Path(raw)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return shutil.which("docker")


def _docker(args: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    exe = _docker_exe()
    if not exe:
        return subprocess.CompletedProcess(
            ["docker", *args],
            127,
            "",
            "docker CLI not found in PATH (install Docker or set ORCH_DOCKER_BIN).",
        )
    try:
        return subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ,
        )
    except OSError as e:
        return subprocess.CompletedProcess([exe, *args], 1, "", str(e))
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(
            [exe, *args], 1, "", f"timeout: {e}"
        )


def docker_probe() -> tuple[bool, dict[str, Any]]:
    """Run `docker info` and return (ok, details for /api/health)."""
    detail: dict[str, Any] = {
        "docker_host": os.environ.get("DOCKER_HOST") or None,
        "docker_context": os.environ.get("DOCKER_CONTEXT") or None,
        "docker_binary": _docker_exe(),
    }
    r = _docker(["info"], timeout=8.0)
    if r.returncode == 0:
        line = (r.stdout or "").splitlines()
        detail["server_hint"] = line[0][:240] if line else ""
        return True, detail
    err = (r.stderr or r.stdout or "").strip() or f"exit code {r.returncode}"
    detail["error"] = err[:1200]
    return False, detail


def docker_available() -> bool:
    ok, _ = docker_probe()
    return ok


def _parse_host_port(docker_port_line: str) -> int | None:
    # "0.0.0.0:32768" or "[::]:32768"
    m = re.search(r":(\d+)\s*$", docker_port_line.strip())
    if not m:
        return None
    return int(m.group(1))


def container_host_port(container_id: str, internal_port: int) -> int | None:
    r = _docker(["port", container_id, f"{internal_port}/tcp"], timeout=10.0)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return _parse_host_port(r.stdout.splitlines()[0])


def inspect_running(container_id: str) -> bool | None:
    r = _docker(
        ["inspect", "-f", "{{.State.Running}}", container_id],
        timeout=10.0,
    )
    if r.returncode != 0:
        return None
    out = r.stdout.strip().lower()
    if out == "true":
        return True
    if out == "false":
        return False
    return None


def _docker_signal_agent_process(container_id: str, pause: bool) -> tuple[bool, str]:
    """
    Pause or resume the `python -m agent` process inside the container via SIGSTOP / SIGCONT.
    """
    sig = "STOP" if pause else "CONT"
    inner = (
        'lines=$(pgrep -f "[p]ython.*-m agent" || true); '
        'if [ -z "$lines" ]; then echo "agent process not found" >&2; exit 1; fi; '
        f'echo "$lines" | xargs kill -{sig}; echo ok'
    )
    r = _docker(["exec", container_id, "bash", "-lc", inner], timeout=25.0)
    err = ((r.stderr or "").strip() + "\n" + (r.stdout or "").strip()).strip()
    if r.returncode != 0:
        return False, err or f"docker exec failed (exit {r.returncode})"
    return True, ""


def _valid_job_url(url: str) -> bool:
    u = (url or "").strip()
    return u.startswith("http://") or u.startswith("https://")


def _host_path(repo_relative: str) -> Path:
    """
    Return a path that is valid on the *Docker host* filesystem.

    The orchestrator backend usually runs inside a container. When it executes `docker run`
    against the host daemon, bind-mount source paths must exist on the host filesystem.

    Set ORCH_HOST_REPO_ROOT to the absolute repo path on the host (e.g. /home/me/octopilot)
    so we can mount host files into agent containers reliably.
    """
    base = Path(HOST_REPO_ROOT) if HOST_REPO_ROOT else REPO_ROOT
    return (base / repo_relative).resolve()


def _mountable_paths(repo_relative: str) -> tuple[Path, Path]:
    """
    Return (check_path, mount_path).

    - check_path: a path that should exist *inside the orchestrator container* (used for validation)
    - mount_path: a path that should exist on the *host* (used for docker bind-mounts)
    """
    return (REPO_ROOT / repo_relative).resolve(), _host_path(repo_relative)


def _attachments_bind() -> tuple[str, str]:
    """
    Host bind source and agent container path for applicant files (resumes, etc.).

    profiles_db stores paths relative to repo `attachments/`; the agent resolves them under
    /attachments inside the machine container.
    """
    check, host = _mountable_paths("attachments")
    check.mkdir(parents=True, exist_ok=True)
    return str(host), "/attachments"


def _fmt_uptime(seconds: float) -> str:
    s = max(0, int(seconds))
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _http_ready(url: str | None, timeout_s: float = 1.0) -> bool:
    """
    Best-effort readiness probe for iframe targets.
    We do it server-side to avoid browser CORS restrictions.
    """
    if not url:
        return False
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "okto-orchestrator/1.0"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # nosec - internal URLs only
            code = int(getattr(resp, "status", 200) or 200)
            return 200 <= code < 400
    except (urllib.error.HTTPError,) as e:
        return 200 <= int(getattr(e, "code", 0) or 0) < 400
    except Exception:
        return False


def _http_ready_any(urls: list[str | None], timeout_s: float = 1.0) -> bool:
    for u in urls:
        if _http_ready(u, timeout_s=timeout_s):
            return True
    return False


def _running_in_docker() -> bool:
    # Presence of /.dockerenv is a common, cheap signal.
    try:
        return Path("/.dockerenv").is_file()
    except Exception:
        return False


def _probe_url(public_url: str | None) -> str | None:
    """
    Convert a public iframe URL into a URL that is reachable *from the orchestrator backend*.

    When the backend runs inside Docker, `127.0.0.1` / `localhost` points at the backend
    container, not the host where the published ports live. In that case, probe via
    `host.docker.internal` (works because we add it in docker run and most Docker Desktop
    setups; on Linux we also add host-gateway for agent containers, and compose typically
    provides it as well).
    """
    if not public_url:
        return None
    try:
        probe_host = (os.environ.get("ORCH_PROBE_HOST") or "").strip() or None
        if probe_host:
            return re.sub(r"^http://[^/]+", f"http://{probe_host}", public_url)
        if not _running_in_docker():
            return public_url
        if public_url.startswith("http://127.0.0.1") or public_url.startswith("http://localhost"):
            return re.sub(r"^http://(127\.0\.0\.1|localhost)", "http://host.docker.internal", public_url)
        return public_url
    except Exception:
        return public_url


def _probe_url_candidates(public_url: str | None) -> list[str | None]:
    """
    Return a small list of plausible probe URLs to check from inside the backend container.
    """
    if not public_url:
        return [None]
    out: list[str | None] = []
    # 1) Explicit override wins.
    override = (os.environ.get("ORCH_PROBE_HOST") or "").strip() or None
    if override:
        out.append(re.sub(r"^http://[^/]+", f"http://{override}", public_url))
        return out

    # 2) Direct probe (works when backend runs on the host).
    out.append(public_url)

    # 3) Common Docker-from-container -> host options.
    if _running_in_docker() and (
        public_url.startswith("http://127.0.0.1") or public_url.startswith("http://localhost")
    ):
        out.append(re.sub(r"^http://(127\.0\.0\.1|localhost)", "http://host.docker.internal", public_url))
        # Fallback for many Linux Docker setups (docker0 gateway).
        out.append(re.sub(r"^http://(127\.0\.0\.1|localhost)", "http://172.17.0.1", public_url))
    return out


def machine_public_view(m: Machine, now: float) -> dict[str, Any]:
    uptime_s = 0.0
    cost = 0.0
    if m.status == "running" and m.started_at:
        uptime_s = max(0.0, now - m.started_at)
        # Prefer progressively reported token-based cost when available.
        if m.llm_cost_usd is not None:
            cost = float(m.llm_cost_usd)
        else:
            cost = (uptime_s / 3600.0) * COST_PER_HOUR_USD
    elif m.status == "stopped" and m.session_cost_usd is not None:
        cost = float(m.session_cost_usd)
        if m.started_at and m.stopped_at:
            uptime_s = max(0.0, m.stopped_at - m.started_at)

    # URLs can be known before the iframe pages are fully ready.
    desktop_url = None
    terminal_url = None
    if m.desktop_port:
        desktop_url = f"http://{PUBLIC_HOST}:{m.desktop_port}/vnc_lite.html"
    if m.terminal_port:
        terminal_url = f"http://{PUBLIC_HOST}:{m.terminal_port}/term/"

    desktop_ready = bool(desktop_url) and _http_ready_any(_probe_url_candidates(desktop_url))
    terminal_ready = bool(terminal_url) and _http_ready_any(_probe_url_candidates(terminal_url))

    image = (m.image or "").strip()
    # Display "latest" if the machine is running the latest tag; otherwise display tag/version.
    image_label = "latest"
    if image and ":" in image:
        tag = image.rsplit(":", 1)[-1].strip()
        if tag and tag.lower() != "latest":
            image_label = tag

    return {
        "id": m.id,
        "job_url": m.job_url,
        "profile_id": m.profile_id,
        "llm_model": m.llm_model,
        "image": image or None,
        "image_label": image_label,
        "status": m.status,
        "error": m.error,
        "container_id": m.container_id,
        "uptime_seconds": round(uptime_s, 1),
        "uptime_label": _fmt_uptime(uptime_s),
        "cost_usd": round(cost, 4),
        "cost_per_hour_usd": COST_PER_HOUR_USD,
        "desktop_url": desktop_url,
        "terminal_url": terminal_url,
        "desktop_ready": desktop_ready,
        "terminal_ready": terminal_ready,
        "view_width": AGENT_VIEW_W,
        "view_height": AGENT_VIEW_H,
        "applications_submitted": m.applications_submitted,
        "llm_tokens": m.llm_tokens,
        "llm_cost_usd": m.llm_cost_usd,
        "needs_human": bool(m.needs_human),
        "needs_human_reason": m.needs_human_reason,
        "needs_human_at": m.needs_human_at,
        "agent_paused": bool(m.agent_paused),
        # agent_state is reported by the agent itself via telemetry and reflects
        # what the in-container agent loop is actually doing ("running",
        # "paused", "stopping"). Unlike `agent_paused` (which is what the
        # orchestrator asked for), this is the observed state.
        "agent_state": m.agent_state,
        "agent_state_at": m.agent_state_at,
    }


def sync_machines_from_docker(data: dict[str, Any]) -> None:
    now = time.time()
    changed = False
    machines: list[dict[str, Any]] = list(data.get("machines") or [])
    for row in machines:
        cid = row.get("container_id")
        st = row.get("status")
        if not cid or st in ("error", "stopped"):
            continue
        running = inspect_running(cid)
        if running is False:
            row["status"] = "stopped"
            row["stopped_at"] = row.get("stopped_at") or now
            row["agent_paused"] = False
            row["agent_state"] = None
            row["agent_state_at"] = None
            if row.get("session_cost_usd") is None:
                # Prefer token-based machine cost if present; else fall back to uptime-based cost.
                if isinstance(row.get("llm_cost_usd"), (int, float)):
                    row["session_cost_usd"] = round(float(row["llm_cost_usd"]), 4)
                elif row.get("started_at"):
                    up = float(row["stopped_at"]) - float(row["started_at"])
                    row["session_cost_usd"] = round(max(0.0, up / 3600.0) * COST_PER_HOUR_USD, 4)
            changed = True
        elif running is True:
            if st == "starting":
                dp = container_host_port(cid, 6080)
                tp = container_host_port(cid, 7681)
                if dp and tp:
                    row["desktop_port"] = dp
                    row["terminal_port"] = tp
                    row["status"] = "running"
                    row["started_at"] = row.get("started_at") or now
                    changed = True
    if changed:
        data["machines"] = machines
        save_state(data)


_state_lock = threading.Lock()
_sync_thread_started = False
_dispatcher_thread_started = False


# ---------------------------------------------------------------------------
# Settings (persisted under data["settings"] in state.json)
# ---------------------------------------------------------------------------

DEFAULT_MAX_PARALLEL_MACHINES = int(os.environ.get("ORCH_MAX_PARALLEL_MACHINES", "4") or 4)
SOURCE_API = (os.environ.get("SOURCE_API") or "").rstrip("/")
QUEUE_DISPATCH_INTERVAL_S = float(os.environ.get("ORCH_QUEUE_DISPATCH_INTERVAL_S", "3.0") or 3.0)


def _default_settings() -> dict[str, Any]:
    return {
        "max_parallel_machines": DEFAULT_MAX_PARALLEL_MACHINES,
        "source_api": SOURCE_API,
        "default_llm_model": DEFAULT_OPENAI_MODEL,
        # Budget controls (0/None means "disabled").
        # Global budget: when exceeded, block BOTH auto-start and manual start.
        "budget_alert_usd": None,
        # Per-machine budget: when exceeded, pause agent and require manual unpause.
        "max_budget_per_machine_usd": None,
        # Model pricing overrides injected into agent containers as AGENT_LLM_PRICING_JSON.
        # Shape: {"models": {"gpt-4.1": {"usd_per_1m_input": 2.0, "usd_per_1m_output": 8.0, ...}, ...}}
        "llm_pricing_overrides": None,
        "llm_ledger_relpath": DEFAULT_LLM_LEDGER_REL,
    }


def _get_settings(data: dict[str, Any]) -> dict[str, Any]:
    s = data.get("settings")
    if not isinstance(s, dict):
        s = {}
    merged = _default_settings()
    for k, v in s.items():
        if k in merged and v is not None:
            merged[k] = v
    return merged


def _update_settings(patch: dict[str, Any]) -> dict[str, Any]:
    with _state_lock:
        data = load_state()
        current = _get_settings(data)
        if "max_parallel_machines" in patch:
            try:
                mp = int(patch["max_parallel_machines"])
            except (TypeError, ValueError):
                raise ValueError("max_parallel_machines must be an integer")
            current["max_parallel_machines"] = max(0, min(64, mp))
        if "source_api" in patch:
            v = str(patch.get("source_api") or "").strip()
            current["source_api"] = v.rstrip("/")
        if "default_llm_model" in patch:
            v = str(patch.get("default_llm_model") or "").strip()
            if not v:
                raise ValueError("default_llm_model must not be empty")
            if not _valid_openai_model_id(v):
                raise ValueError("default_llm_model is not a valid OpenAI model id")
            current["default_llm_model"] = v
        if "budget_alert_usd" in patch:
            v = patch.get("budget_alert_usd")
            if v is None or v == "":
                current["budget_alert_usd"] = None
            elif isinstance(v, (int, float)):
                current["budget_alert_usd"] = max(0.0, float(v))
            else:
                raise ValueError("budget_alert_usd must be a number or null")
        if "max_budget_per_machine_usd" in patch:
            v = patch.get("max_budget_per_machine_usd")
            if v is None or v == "":
                current["max_budget_per_machine_usd"] = None
            elif isinstance(v, (int, float)):
                current["max_budget_per_machine_usd"] = max(0.0, float(v))
            else:
                raise ValueError("max_budget_per_machine_usd must be a number or null")
        if "llm_pricing_overrides" in patch:
            v = patch.get("llm_pricing_overrides")
            if v is None or v == "":
                current["llm_pricing_overrides"] = None
            elif isinstance(v, dict):
                # Store raw dict; agent validates keys it understands.
                current["llm_pricing_overrides"] = v
            else:
                raise ValueError("llm_pricing_overrides must be an object or null")
        if "llm_ledger_relpath" in patch:
            v = str(patch.get("llm_ledger_relpath") or "").strip()
            if v:
                current["llm_ledger_relpath"] = v
        data["settings"] = current
        save_state(data)
        return current


# ---------------------------------------------------------------------------
# Source API HTTP helpers (stdlib urllib to avoid extra deps)
# ---------------------------------------------------------------------------


def _src_request(method: str, path: str, *, body: Any = None, timeout: float = 5.0, base: str | None = None) -> Any:
    base_url = (base or SOURCE_API or "").rstrip("/")
    if not base_url:
        raise RuntimeError("SOURCE_API is not configured")
    url = f"{base_url}{path}"
    data_bytes = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data_bytes = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data_bytes, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted internal URL)
        raw = resp.read()
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return None


def _src_list_queue(status: str, *, base: str | None = None) -> list[dict[str, Any]]:
    try:
        rows = _src_request(
            "GET", f"/api/queue?status={urllib.parse.quote(status)}", base=base, timeout=5.0
        )
    except Exception:
        return []
    return rows if isinstance(rows, list) else []


def _src_get_job(job_id: str, *, base: str | None = None) -> dict[str, Any] | None:
    try:
        row = _src_request("GET", f"/api/jobs/{urllib.parse.quote(job_id)}", base=base, timeout=5.0)
    except Exception:
        return None
    return row if isinstance(row, dict) else None


def _src_find_job_by_url(url: str, *, base: str | None = None) -> dict[str, Any] | None:
    """
    Find a job posting that matches the given application URL. We try exact `url`
    and `apply_url` matches first; when the URL carries a query string we also fall
    back to host+path comparison so tracking parameters don't break the lookup.
    """
    u = (url or "").strip()
    if not u:
        return None
    try:
        rows = _src_request("GET", "/api/jobs", base=base, timeout=6.0)
    except Exception:
        return None
    if not isinstance(rows, list):
        return None

    def _canon(x: str) -> str:
        try:
            p = urllib.parse.urlsplit((x or "").strip())
            return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")
        except Exception:
            return (x or "").strip().rstrip("/")

    target = _canon(u)
    best: dict[str, Any] | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in ("apply_url", "url"):
            v = (row.get(key) or "").strip()
            if not v:
                continue
            if v == u:
                return row
            if _canon(v) == target:
                best = row
                break
    return best


def _src_patch_queue(qid: str, patch: dict[str, Any], *, base: str | None = None) -> bool:
    try:
        _src_request("PATCH", f"/api/queue/{urllib.parse.quote(qid)}", body=patch, base=base, timeout=5.0)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Queue dispatcher: pulls pending items from the configured SOURCE_API and
# spawns machines whenever there is room below the configured max parallel count.
# ---------------------------------------------------------------------------


def _queue_dispatch_tick() -> None:
    # 1) Sync known machines from docker so the active count is accurate.
    with _state_lock:
        data = load_state()
        sync_machines_from_docker(data)
        settings = _get_settings(data)
        # Global budget lock: stop all auto-dispatch when exceeded.
        if _budget_exceeded_locked(data):
            return
        max_parallel = int(settings.get("max_parallel_machines") or 0)
        base = settings.get("source_api") or SOURCE_API
        default_llm_model = settings.get("default_llm_model") or DEFAULT_OPENAI_MODEL
        machines: list[dict[str, Any]] = list(data.get("machines") or [])
    machine_by_id = {m.get("id"): m for m in machines if m.get("id")}
    active = sum(1 for m in machines if m.get("status") in ("starting", "running"))

    # Without a configured source API there is nothing to dispatch.
    if not base:
        return

    # 2) Reconcile in_progress queue items whose machines have stopped / errored.
    for it in _src_list_queue("in_progress", base=base):
        mid = (it.get("machine_id") or "").strip()
        if not mid:
            continue
        m = machine_by_id.get(mid)
        if not m:
            continue
        st = m.get("status")
        if st == "stopped":
            _src_patch_queue(it["id"], {"status": "done"}, base=base)
        elif st == "error":
            err_text = (m.get("error") or "agent container errored")[:500]
            _src_patch_queue(it["id"], {"status": "error", "error": err_text}, base=base)

    # 3) If there is room, dispatch pending items (sorted by priority desc already by backend).
    slots = max(0, max_parallel - active)
    if slots <= 0:
        return
    pending = _src_list_queue("pending", base=base)
    for it in pending[:slots]:
        # Claim the item first (so concurrent tickers don't double-spawn).
        if not _src_patch_queue(it["id"], {"status": "in_progress"}, base=base):
            continue
        job = _src_get_job(it.get("job_id") or "", base=base)
        if not job:
            _src_patch_queue(
                it["id"], {"status": "error", "error": "job not found in source api"}, base=base
            )
            continue
        url = (job.get("apply_url") or "").strip() or (job.get("url") or "").strip()
        if not url:
            _src_patch_queue(
                it["id"], {"status": "error", "error": "job has no url / apply_url"}, base=base
            )
            continue
        job_title = (job.get("title") or "").strip()
        job_company = (job.get("company") or "").strip()
        job_city = (job.get("city") or "").strip()
        row, err = _spawn_machine_row(
            url=url,
            profile_id=(it.get("profile_id") or DEFAULT_PROFILE),
            llm_model=default_llm_model,
            job_title=job_title,
            job_company=job_company,
            job_city=job_city,
        )
        if err and (row is None or row.get("status") == "error"):
            _src_patch_queue(
                it["id"],
                {
                    "status": "error",
                    "error": err[:500],
                    "machine_id": (row or {}).get("id") or "",
                },
                base=base,
            )
            continue
        assert row is not None
        _src_patch_queue(it["id"], {"machine_id": row["id"]}, base=base)


def _background_sync_loop() -> None:
    while True:
        time.sleep(3.0)
        with _state_lock:
            data = load_state()
            sync_machines_from_docker(data)
            _enforce_budget_limits_locked(data)


def _compute_total_cost_usd(machines: list[Machine], now: float) -> float:
    total_cost = 0.0
    for m in machines:
        if m.status == "running" and m.started_at:
            if m.llm_cost_usd is not None:
                total_cost += float(m.llm_cost_usd)
            else:
                uptime_s = max(0.0, now - float(m.started_at))
                total_cost += (uptime_s / 3600.0) * COST_PER_HOUR_USD
        elif m.status == "stopped" and m.session_cost_usd is not None:
            total_cost += float(m.session_cost_usd)
    return float(total_cost)


def _budget_exceeded_locked(data: dict[str, Any]) -> bool:
    s = _get_settings(data)
    limit = s.get("budget_alert_usd")
    try:
        lim = float(limit) if isinstance(limit, (int, float)) else None
    except Exception:
        lim = None
    if lim is None or lim <= 0:
        return False
    machines = [_machine_from_dict(x) for x in (data.get("machines") or [])]
    total_cost = _compute_total_cost_usd(machines, time.time())
    return total_cost >= lim


def _enforce_budget_limits_locked(data: dict[str, Any]) -> None:
    """
    Enforce per-machine budget caps by pausing the agent process and marking the
    machine as needing human intervention. This runs in the background sync loop.
    """
    s = _get_settings(data)
    cap = s.get("max_budget_per_machine_usd")
    try:
        capf = float(cap) if isinstance(cap, (int, float)) else None
    except Exception:
        capf = None
    if capf is None or capf <= 0:
        return

    machines: list[dict[str, Any]] = list(data.get("machines") or [])
    changed = False
    now = time.time()
    for row in machines:
        if row.get("status") != "running":
            continue
        if row.get("agent_paused"):
            continue
        # Per requirements: use llm_cost_usd only; if absent, treat as 0.
        llm_cost = row.get("llm_cost_usd")
        if not isinstance(llm_cost, (int, float)):
            continue
        if float(llm_cost) < capf:
            continue

        cid = row.get("container_id")
        if not cid or inspect_running(cid) is not True:
            continue

        # Primary mechanism: cooperative pause via the control file. The
        # agent blocks before its next LLM call. SIGSTOP is attempted as a
        # best-effort responsiveness boost only.
        mid = row.get("id")
        wrote_control = False
        try:
            if isinstance(mid, str) and mid:
                _write_control_state(mid, state="paused")
                wrote_control = True
        except OSError:
            wrote_control = False

        _docker_signal_agent_process(str(cid), pause=True)

        if not wrote_control:
            row["needs_human"] = True
            row["needs_human_reason"] = "budget_per_machine_exceeded_pause_failed"
            row["needs_human_at"] = row.get("needs_human_at") or now
            changed = True
            continue

        row["agent_paused"] = True
        row["agent_state"] = "paused"
        row["agent_state_at"] = now
        row["needs_human"] = True
        row["needs_human_reason"] = "budget_per_machine_exceeded"
        row["needs_human_at"] = row.get("needs_human_at") or now
        changed = True

    if changed:
        data["machines"] = machines
        save_state(data)


def _background_queue_dispatch_loop() -> None:
    # Initial delay lets the module finish importing before the first tick
    # references `_spawn_machine_row` (which is defined later in this module).
    time.sleep(max(1.0, QUEUE_DISPATCH_INTERVAL_S))
    while True:
        try:
            _queue_dispatch_tick()
        except Exception:
            # Never let the dispatcher thread die on transient errors.
            pass
        time.sleep(max(1.0, QUEUE_DISPATCH_INTERVAL_S))


def ensure_background_sync() -> None:
    global _sync_thread_started, _dispatcher_thread_started
    if not _sync_thread_started:
        _sync_thread_started = True
        t = threading.Thread(target=_background_sync_loop, daemon=True)
        t.start()
    if not _dispatcher_thread_started:
        _dispatcher_thread_started = True
        d = threading.Thread(target=_background_queue_dispatch_loop, daemon=True)
        d.start()


app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Start background sync + queue dispatcher as soon as the app is imported
# so they run under gunicorn too (not only in the __main__ branch).
ensure_background_sync()


@app.get("/")
def index_page():
    index = FRONTEND_DIST / "index.html"
    if index.is_file():
        return send_from_directory(FRONTEND_DIST, "index.html")
    return (
        jsonify(
            {
                "message": "Orchestrator API is running. Build the UI with "
                "`cd orchestrator/frontend && npm install && npm run build`, "
                "or run `npm run dev` (proxies /api to port 5050).",
                "health": "/api/health",
            }
        ),
        200,
    )


@app.get("/assets/<path:asset_path>")
def vite_assets(asset_path):
    """Serve Vite build chunks when `npm run build` has been run."""
    d = FRONTEND_DIST / "assets"
    target = d / asset_path
    if not target.resolve().is_relative_to(d.resolve()):
        return jsonify({"error": "not found"}), 404
    if target.is_file():
        return send_from_directory(d, asset_path)
    return jsonify({"error": "not found"}), 404


@app.get("/api/health")
def health():
    ok, dinfo = docker_probe()
    return jsonify(
        {
            "ok": True,
            "docker": ok,
            "docker_detail": dinfo,
            "repo_root": str(REPO_ROOT),
            "image": DEFAULT_IMAGE,
        }
    )


@app.get("/api/settings")
def get_settings():
    ensure_background_sync()
    with _state_lock:
        data = load_state()
        settings = _get_settings(data)
    return jsonify({"settings": settings})


@app.patch("/api/settings")
def patch_settings():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "body must be a JSON object"}), 400
    try:
        settings = _update_settings(body)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"settings": settings})


# ---------------------------------------------------------------------------
# LLM provider keys (stored in MongoDB; injected into agent containers as -e flags)
# ---------------------------------------------------------------------------

@app.get("/api/llm-providers")
def list_llm_providers():
    """
    List configured LLM providers (with masked keys), plus the catalog of
    providers the user can add. Returns:
      {
        "providers": [{provider, label, env_var, key_masked, key_last4, key_length,
                       key_set, models[], spend_usd, created_at, updated_at}, ...],
        "available": [{id, label, env_var}, ...]   # everything in the catalog
      }
    """
    try:
        providers = _list_llm_providers_public()
    except PyMongoError as e:
        return jsonify({"error": f"MongoDB error: {e}"}), 503
    available = [
        {"id": p["id"], "label": p["label"], "env_var": p["env_var"]}
        for p in LLM_PROVIDER_REGISTRY
    ]
    return jsonify({"providers": providers, "available": available})


@app.post("/api/llm-providers")
def upsert_llm_provider():
    """
    Create or update a provider's API key. Body:
      { "provider": "openai", "api_key": "sk-..." }
    Returns the (key-redacted) public view of the saved row.
    """
    body = request.get_json(silent=True) or {}
    pid = str(body.get("provider") or "").strip().lower()
    api_key = str(body.get("api_key") or "").strip()
    if not pid or pid not in LLM_PROVIDER_INDEX:
        return jsonify({"error": "provider must be one of: " + ", ".join(LLM_PROVIDER_INDEX)}), 400
    if not api_key:
        return jsonify({"error": "api_key must be a non-empty string"}), 400
    if len(api_key) > 1024:
        return jsonify({"error": "api_key is unreasonably long"}), 400

    now = _utc_iso()
    try:
        coll = _llm_providers_coll()
        coll.update_one(
            {"provider": pid},
            {
                "$set": {
                    "provider": pid,
                    "api_key": api_key,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        doc = coll.find_one({"provider": pid}) or {"provider": pid, "api_key": api_key}
    except PyMongoError as e:
        return jsonify({"error": f"MongoDB error: {e}"}), 503

    return jsonify({"provider": _serialize_llm_provider(doc)})


@app.delete("/api/llm-providers/<provider>")
def delete_llm_provider(provider: str):
    """Remove a provider row (and its stored key)."""
    pid = (provider or "").strip().lower()
    if not pid or pid not in LLM_PROVIDER_INDEX:
        return jsonify({"error": "unknown provider"}), 404
    try:
        coll = _llm_providers_coll()
        res = coll.delete_one({"provider": pid})
    except PyMongoError as e:
        return jsonify({"error": f"MongoDB error: {e}"}), 503
    if not res.deleted_count:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True, "provider": pid})


@app.post("/api/reconcile/openai")
def reconcile_openai_usage():
    """
    Run the OpenAI usage reconciliation script server-side.

    Requires an admin key via `X-OpenAI-Admin-Key` header or OPENAI_ADMIN_KEY env var.
    Expects JSON body:
      { "start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "ledger_relpath": "..." (optional) }
    """
    body = request.get_json(silent=True) or {}
    start = (body.get("start") or "").strip()
    end = (body.get("end") or "").strip()
    ledger_rel = (body.get("ledger_relpath") or "").strip()

    admin_key = (
        (request.headers.get("X-OpenAI-Admin-Key") or "").strip()
        or _get_llm_provider_key("openai-admin")
        or (os.environ.get("OPENAI_ADMIN_KEY") or "").strip()
    )
    if not admin_key:
        return jsonify({"error": "Missing OpenAI admin key (configure it in Settings → LLM providers, or send X-OpenAI-Admin-Key)."}), 400
    if not start or not end:
        return jsonify({"error": "Provide start and end (YYYY-MM-DD)."}), 400

    with _state_lock:
        data = load_state()
        settings = _get_settings(data)
        ledger_rel = ledger_rel or (settings.get("llm_ledger_relpath") or DEFAULT_LLM_LEDGER_REL)

    # Ledger is now stored in Mongo; the reconcile script expects a JSONL file.
    return jsonify({"error": "OpenAI reconcile expects a JSONL ledger file; ledger is stored in Mongo in this setup."}), 400

    env = dict(os.environ)
    env["OPENAI_ADMIN_KEY"] = admin_key

    cmd = [
        shutil.which("python3") or "python3",
        "-m",
        "agent.reconcile_openai_usage",
        "--ledger",
        str(ledger_check),
        "--start",
        start,
        "--end",
        end,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=90.0, cwd=str(REPO_ROOT))
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Reconcile timed out."}), 504

    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        return jsonify({"error": err or out or "Reconcile failed.", "stdout": out, "stderr": err}), 400
    return jsonify({"ok": True, "stdout": out, "stderr": err})


@app.get("/api/ledger")
def get_ledger():
    """
    Return recent LLM ledger rows (Mongo) along with any saved overrides.

    Query params:
      limit=<int> (default 200, max 2000)
    """
    limit_raw = request.args.get("limit", "200")
    try:
        limit = max(1, min(2000, int(limit_raw)))
    except Exception:
        limit = 200

    with _state_lock:
        data = load_state()
        settings = _get_settings(data)
        ledger_rel = (settings.get("llm_ledger_relpath") or DEFAULT_LLM_LEDGER_REL).strip()
        overrides = data.get("ledger_overrides") if isinstance(data.get("ledger_overrides"), dict) else {}
    try:
        cur = _llm_ledger_coll().find({}, {"_id": 0}).sort([("ts_unix", -1)]).limit(limit)
        rows: list[dict[str, Any]] = []
        for obj in cur:
            if not isinstance(obj, dict):
                continue
            rid = str(obj.get("request_id") or obj.get("id") or "").strip()
            ov = overrides.get(rid) if rid and isinstance(overrides, dict) else None
            if isinstance(ov, dict):
                obj = dict(obj)
                obj["override"] = ov
            rows.append(obj)
        return jsonify({"ledger_relpath": ledger_rel, "rows": rows})
    except PyMongoError:
        return jsonify({"error": "Failed to read ledger"}), 500


@app.patch("/api/ledger/overrides")
def patch_ledger_overrides():
    """
    Save per-request overrides for ledger display (does not rewrite JSONL ledger).

    Body:
      { "overrides": { "<request_id>": { "cost_usd": 0.1234, "note": "..." }, ... } }
    """
    body = request.get_json(silent=True) or {}
    overrides = body.get("overrides")
    if not isinstance(overrides, dict):
        return jsonify({"error": "overrides must be an object"}), 400

    cleaned: dict[str, Any] = {}
    for k, v in overrides.items():
        rid = str(k or "").strip()
        if not rid:
            continue
        if v is None:
            cleaned[rid] = None
            continue
        if not isinstance(v, dict):
            continue
        out: dict[str, Any] = {}
        if "cost_usd" in v:
            cv = v.get("cost_usd")
            if cv is None or cv == "":
                out["cost_usd"] = None
            elif isinstance(cv, (int, float)):
                out["cost_usd"] = float(cv)
            else:
                return jsonify({"error": f"override cost_usd for {rid} must be a number or null"}), 400
        if "note" in v:
            nv = v.get("note")
            out["note"] = (str(nv)[:500] if nv is not None else "")
        cleaned[rid] = out

    with _state_lock:
        data = load_state()
        cur = data.get("ledger_overrides")
        if not isinstance(cur, dict):
            cur = {}
        for rid, ov in cleaned.items():
            if ov is None:
                cur.pop(rid, None)
            else:
                cur[rid] = ov
        data["ledger_overrides"] = cur
        save_state(data)

    return jsonify({"ok": True, "count": len(cleaned)})


@app.get("/api/stats")
def stats():
    ensure_background_sync()
    with _state_lock:
        data = load_state()
        sync_machines_from_docker(data)
        machines = [_machine_from_dict(x) for x in data.get("machines") or []]
        now = time.time()
        running = sum(1 for m in machines if m.status == "running")
        starting = sum(1 for m in machines if m.status == "starting")
        total_cost = _compute_total_cost_usd(machines, now)
        extra = int(data.get("total_applications_submitted") or 0)
        per_m = sum(m.applications_submitted for m in machines)
        settings = _get_settings(data)
        budget_limit = settings.get("budget_alert_usd")
        budget_exceeded = False
        if isinstance(budget_limit, (int, float)) and float(budget_limit) > 0:
            budget_exceeded = float(total_cost) >= float(budget_limit)
        return jsonify(
            {
                "machines_running": running,
                "machines_starting": starting,
                "machines_total": len(machines),
                "total_cost_usd": round(total_cost, 4),
                "applications_submitted": extra + per_m,
                "cost_per_hour_usd": COST_PER_HOUR_USD,
                "budget_alert_usd": budget_limit,
                "budget_exceeded": budget_exceeded,
            }
        )


@app.get("/api/machines")
def list_machines():
    ensure_background_sync()
    with _state_lock:
        data = load_state()
        sync_machines_from_docker(data)
        machines = [_machine_from_dict(x) for x in data.get("machines") or []]
        now = time.time()
        return jsonify([machine_public_view(m, now) for m in machines])


@app.get("/api/machines/<mid>")
def get_machine(mid: str):
    ensure_background_sync()
    with _state_lock:
        data = load_state()
        sync_machines_from_docker(data)
        for row in data.get("machines") or []:
            if row.get("id") == mid:
                return jsonify(machine_public_view(_machine_from_dict(row), time.time()))
        return jsonify({"error": "not found"}), 404


@app.get("/api/machines/<mid>/terminal-logs")
def get_machine_terminal_logs(mid: str):
    """Return captured tmux agent-pane text for this machine (append-only across restarts)."""
    ensure_background_sync()
    with _state_lock:
        data = load_state()
        if not any(r.get("id") == mid for r in (data.get("machines") or [])):
            return jsonify({"error": "not found"}), 404
    text, truncated, total_bytes = _read_terminal_log(mid)
    return jsonify(
        {
            "text": text,
            "truncated": truncated,
            "total_bytes": total_bytes,
            "max_bytes": TERMINAL_LOG_MAX_BYTES,
        }
    )


def _spawn_machine_row(
    *,
    url: str,
    profile_id: str,
    image: str | None = None,
    llm_model: str | None = None,
    job_title: str | None = None,
    job_company: str | None = None,
    job_city: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Core docker spawn logic shared by the HTTP endpoint and the queue dispatcher.
    Returns (machine_row, error_message). On error, machine_row may still be set
    (an `error` row recorded in state) along with error_message.
    """
    if not docker_available():
        return None, "Docker is not available (is the daemon running?)"
    if not _valid_job_url(url):
        return None, "Provide a valid https?:// job application URL."
    profile_id = (profile_id or DEFAULT_PROFILE).strip()
    image = (image or DEFAULT_IMAGE).strip()
    llm_model = (llm_model or DEFAULT_OPENAI_MODEL).strip() or DEFAULT_OPENAI_MODEL
    if not _valid_openai_model_id(llm_model):
        return None, "Invalid `llm_model` (expected a safe OpenAI model id)."
    jt = (job_title or "").strip()
    jc = (job_company or "").strip()
    jcity = (job_city or "").strip()

    entrypoint_mount = _host_path("agent/docker/entrypoint.sh")
    attachments_host, attachments_ctr = _attachments_bind()

    mid = str(uuid.uuid4())
    name = f"octopilot-orch-{mid[:12]}"

    # Read settings once (pricing overrides + ledger path) for env injection.
    with _state_lock:
        data_for_settings = load_state()
        settings = _get_settings(data_for_settings)
    pricing_overrides = settings.get("llm_pricing_overrides")
    llm_env_args = _llm_provider_env_args()

    cmd = [
        "run",
        "-d",
        "--name",
        name,
        "--add-host",
        "host.docker.internal:host-gateway",
        "-p",
        "0:6080",
        "-p",
        "0:7681",
        "-v",
        f"{attachments_host}:{attachments_ctr}:ro",
        "-e",
        f"AGENT_VIEW_WIDTH={AGENT_VIEW_W}",
        "-e",
        f"AGENT_VIEW_HEIGHT={AGENT_VIEW_H}",
        "-e",
        "AGENT_ATTACHMENTS_DIR=/attachments",
        "-e",
        f"ORCH_MACHINE_ID={mid}",
        "-e",
        "ORCH_API_BASE=http://host.docker.internal:5050",
    ]
    cmd.extend(_agent_runtime_env_for_model(llm_model))
    # LLM ledger is stored in Mongo via the orchestrator API (no on-disk JSONL file).
    cmd.extend(["-e", "AGENT_LLM_LEDGER_URL=http://host.docker.internal:5050/api/llm-ledger/append"])
    if isinstance(pricing_overrides, dict) and pricing_overrides:
        cmd.extend(["-e", f"AGENT_LLM_PRICING_JSON={json.dumps(pricing_overrides)}"])
    cmd.extend(llm_env_args)
    cmd.extend(["-v", f"{entrypoint_mount}:/usr/local/bin/agent-entrypoint.sh:ro"])
    host_log, ctr_log = _prepare_terminal_log_bind(mid, "created")
    cmd.extend(["-v", f"{host_log}:{ctr_log}"])
    # Cooperative control bind-mount (pause / takeover). The agent watches
    # state.json inside this directory before every LLM call.
    control_host, control_ctr = _prepare_control_bind(mid, initial_state="running")
    cmd.extend(["-v", f"{control_host}:{control_ctr}"])
    cmd.extend(["-e", f"AGENT_CONTROL_DIR={control_ctr}"])
    cmd.extend(
        [
            image,
            "python",
            "-m",
            "agent",
            "--url",
            url,
            "--db-profile",
        "--db",
        "/app/profiles_db.json",
            "--profile-id",
            profile_id,
        ]
    )

    with _state_lock:
        data = load_state()
        machines: list[dict[str, Any]] = list(data.get("machines") or [])

        r = _docker(cmd, timeout=180.0)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "docker run failed").strip()
            row = {
                "id": mid,
                "job_url": url,
                "profile_id": profile_id,
                "llm_model": llm_model,
                "job_title": jt,
                "job_company": jc,
                "job_city": jcity,
                "status": "error",
                "error": err[:2000],
                "container_id": None,
                "desktop_port": None,
                "terminal_port": None,
                "started_at": None,
                "applications_submitted": 0,
                "llm_tokens": None,
                "llm_cost_usd": None,
                "agent_paused": False,
                "created_at": time.time(),
            }
            machines.append(row)
            data["machines"] = machines
            save_state(data)
            return row, err[:500]

        cid = r.stdout.strip()
        row = {
            "id": mid,
            "job_url": url,
            "profile_id": profile_id,
            "llm_model": llm_model,
            "job_title": jt,
            "job_company": jc,
            "job_city": jcity,
            "image": image,
            "status": "starting",
            "error": None,
            "container_id": cid,
            "desktop_port": None,
            "terminal_port": None,
            "started_at": time.time(),
            "stopped_at": None,
            "session_cost_usd": None,
            "applications_submitted": 0,
            "llm_tokens": None,
            "llm_cost_usd": None,
            "needs_human": False,
            "needs_human_reason": None,
            "needs_human_at": None,
            "agent_paused": False,
            "created_at": time.time(),
        }
        machines.append(row)
        data["machines"] = machines
        save_state(data)

        # Try to resolve ports quickly (container may need a second)
        for _ in range(30):
            time.sleep(0.3)
            dp = container_host_port(cid, 6080)
            tp = container_host_port(cid, 7681)
            if dp and tp:
                row["desktop_port"] = dp
                row["terminal_port"] = tp
                row["status"] = "running"
                data["machines"] = machines
                save_state(data)
                break
            if inspect_running(cid) is False:
                row["status"] = "error"
                row["error"] = "Container exited before ports were published."
                data["machines"] = machines
                save_state(data)
                return row, row["error"]

        # Create a placeholder application record immediately so humans can
        # review/edit metadata before the agent posts a final result.
        try:
            now = time.time()
            placeholder = {
                "id": str(uuid.uuid4()),
                "machine_id": mid,
                "run_id": None,
                "created_at": now,
                "created_at_iso": _utc_iso(now),
                "application_url": url,
                "job_title": jt,
                "job_company": jc,
                "job_city": jcity,
                "profile_id": profile_id,
                "llm_model": llm_model,
                "status": "In progress",
                "description": "",
                "fields": {},
                "duration_seconds": None,
                "duration_label": None,
                "cost_usd": None,
                "runtime_cost_usd": None,
                "llm_cost_usd": None,
                "llm_tokens": None,
                "screenshots": [],
                "screenshot_count": 0,
                "reviewed": False,
                "raw": {"source": "orchestrator.placeholder"},
            }
            append_application_record(placeholder)
        except Exception:
            pass

        return row, None


@app.post("/api/machines")
def create_machine():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    profile_id = (body.get("profile_id") or DEFAULT_PROFILE).strip()
    image = (body.get("image") or DEFAULT_IMAGE).strip()
    llm_model = (body.get("llm_model") or "").strip() or DEFAULT_OPENAI_MODEL
    job_title = (body.get("job_title") or "").strip()
    job_company = (body.get("job_company") or "").strip()
    job_city = (body.get("job_city") or "").strip()

    ensure_background_sync()
    with _state_lock:
        data = load_state()
        sync_machines_from_docker(data)
        settings = _get_settings(data)
        max_parallel = int(settings.get("max_parallel_machines") or 0)
        active = sum(1 for m in (data.get("machines") or []) if m.get("status") in ("starting", "running"))
        if max_parallel > 0 and active >= max_parallel:
            return jsonify({"error": f"Max parallel machines reached ({active}/{max_parallel})."}), 429
        if _budget_exceeded_locked(data):
            lim = settings.get("budget_alert_usd")
            return jsonify({"error": f"Budget exceeded (limit ${float(lim):.2f}). Manual action required."}), 403

    row, err = _spawn_machine_row(
        url=url,
        profile_id=profile_id,
        image=image,
        llm_model=llm_model,
        job_title=job_title,
        job_company=job_company,
        job_city=job_city,
    )
    if err and (row is None or row.get("status") == "error"):
        payload: dict[str, Any] = {"error": err}
        if row is not None:
            payload["machine"] = machine_public_view(_machine_from_dict(row), time.time())
        # 503 if docker was not available, else 400.
        code = 503 if "Docker is not available" in err else 400
        return jsonify(payload), code
    assert row is not None
    return jsonify(machine_public_view(_machine_from_dict(row), time.time())), 201


@app.delete("/api/machines/<mid>")
def delete_machine(mid: str):
    if not docker_available():
        return jsonify({"error": "Docker is not available"}), 503

    with _state_lock:
        data = load_state()
        machines: list[dict[str, Any]] = list(data.get("machines") or [])
        found = None
        for i, row in enumerate(machines):
            if row.get("id") == mid:
                found = i
                break
        if found is None:
            return jsonify({"error": "not found"}), 404

        row = machines[found]
        cid = row.get("container_id")
        tnow = time.time()
        if cid and row.get("status") not in ("error", "stopped"):
            _docker(["stop", cid], timeout=60.0)
            _docker(["rm", cid], timeout=60.0)

        row["status"] = "stopped"
        row["stopped_at"] = tnow
        if row.get("started_at"):
            up = tnow - float(row["started_at"])
            row["session_cost_usd"] = round(max(0.0, up / 3600.0) * COST_PER_HOUR_USD, 4)
        row["desktop_port"] = None
        row["terminal_port"] = None
        row["agent_paused"] = False
        data["machines"] = machines
        save_state(data)
        return jsonify({"ok": True})


@app.post("/api/machines/<mid>/restart")
def restart_machine(mid: str):
    if not docker_available():
        return jsonify({"error": "Docker is not available"}), 503

    entrypoint_mount = _host_path("agent/docker/entrypoint.sh")
    attachments_host, attachments_ctr = _attachments_bind()
    llm_env_args = _llm_provider_env_args()

    with _state_lock:
        data = load_state()
        machines: list[dict[str, Any]] = list(data.get("machines") or [])
        row = next((r for r in machines if r.get("id") == mid), None)
        if not row:
            return jsonify({"error": "not found"}), 404

        url = (row.get("job_url") or "").strip()
        if not _valid_job_url(url):
            return jsonify({"error": "machine has invalid stored job_url"}), 400

        profile_id = (row.get("profile_id") or DEFAULT_PROFILE).strip()
        image = (row.get("image") or DEFAULT_IMAGE).strip()
        llm_model = (row.get("llm_model") or "").strip() or DEFAULT_OPENAI_MODEL
        if not _valid_openai_model_id(llm_model):
            llm_model = DEFAULT_OPENAI_MODEL

        # Stop/remove existing container if present
        old_cid = row.get("container_id")
        if old_cid:
            _docker(["rm", "-f", str(old_cid)], timeout=90.0)

        name = f"octopilot-orch-{mid[:12]}-{uuid.uuid4().hex[:6]}"
        cmd = [
            "run",
            "-d",
            "--name",
            name,
            "--add-host",
            "host.docker.internal:host-gateway",
            "-p",
            "0:6080",
            "-p",
            "0:7681",
            "-v",
            f"{attachments_host}:{attachments_ctr}:ro",
            "-e",
            f"AGENT_VIEW_WIDTH={AGENT_VIEW_W}",
            "-e",
            f"AGENT_VIEW_HEIGHT={AGENT_VIEW_H}",
            "-e",
            "AGENT_ATTACHMENTS_DIR=/attachments",
            "-e",
            f"ORCH_MACHINE_ID={mid}",
            "-e",
            "ORCH_API_BASE=http://host.docker.internal:5050",
        ]
        cmd.extend(_agent_runtime_env_for_model(llm_model))
        # Inject pricing overrides + ledger path (same as initial spawn).
        settings = _get_settings(data)
        pricing_overrides = settings.get("llm_pricing_overrides")
        # LLM ledger is stored in Mongo via the orchestrator API (no on-disk JSONL file).
        cmd.extend(["-e", "AGENT_LLM_LEDGER_URL=http://host.docker.internal:5050/api/llm-ledger/append"])
        if isinstance(pricing_overrides, dict) and pricing_overrides:
            cmd.extend(["-e", f"AGENT_LLM_PRICING_JSON={json.dumps(pricing_overrides)}"])
        cmd.extend(llm_env_args)
        cmd.extend(["-v", f"{entrypoint_mount}:/usr/local/bin/agent-entrypoint.sh:ro"])
        host_log, ctr_log = _prepare_terminal_log_bind(mid, "restarted")
        cmd.extend(["-v", f"{host_log}:{ctr_log}"])
        # Cooperative control bind-mount (pause / takeover). Reset to a clean
        # "running" state on every restart so a stale pause/takeover file from
        # a previous run does not silently block the fresh agent.
        control_host, control_ctr = _prepare_control_bind(mid, initial_state="running")
        cmd.extend(["-v", f"{control_host}:{control_ctr}"])
        cmd.extend(["-e", f"AGENT_CONTROL_DIR={control_ctr}"])

        cmd.extend(
            [
                image,
                "python",
                "-m",
                "agent",
                "--url",
                url,
                "--db-profile",
                "--db",
                "/app/profiles_db.json",
                "--profile-id",
                profile_id,
            ]
        )

        r = _docker(cmd, timeout=180.0)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "docker run failed").strip()
            row["status"] = "error"
            row["error"] = err[:2000]
            row["container_id"] = None
            row["desktop_port"] = None
            row["terminal_port"] = None
            data["machines"] = machines
            save_state(data)
            return jsonify({"error": err[:500]}), 400

        cid = r.stdout.strip()
        tnow = time.time()
        row["image"] = image
        row["llm_model"] = llm_model
        row["status"] = "starting"
        row["error"] = None
        row["container_id"] = cid
        row["desktop_port"] = None
        row["terminal_port"] = None
        row["started_at"] = tnow
        row["stopped_at"] = None
        row["session_cost_usd"] = None
        row["applications_submitted"] = 0
        row["llm_tokens"] = None
        row["llm_cost_usd"] = None
        row["agent_paused"] = False
        row["agent_state"] = None
        row["agent_state_at"] = None

        # Quick port resolution
        for _ in range(30):
            time.sleep(0.3)
            dp = container_host_port(cid, 6080)
            tp = container_host_port(cid, 7681)
            if dp and tp:
                row["desktop_port"] = dp
                row["terminal_port"] = tp
                row["status"] = "running"
                break
            if inspect_running(cid) is False:
                row["status"] = "error"
                row["error"] = "Container exited before ports were published."
                break

        data["machines"] = machines
        save_state(data)
        return jsonify(machine_public_view(_machine_from_dict(row), time.time()))


@app.post("/api/machines/<mid>/attention")
def set_attention(mid: str):
    """
    Mark a machine as needing human intervention (or clear it).

    Expected JSON:
      { "needed": true, "reason": "captcha" }
      { "needed": false }
    """
    body = request.get_json(silent=True) or {}
    needed = bool(body.get("needed", True))
    reason_raw = body.get("reason")
    reason = (str(reason_raw).strip() if reason_raw is not None else None) or None
    now = time.time()

    with _state_lock:
        data = load_state()
        machines: list[dict[str, Any]] = list(data.get("machines") or [])
        for row in machines:
            if row.get("id") == mid:
                row["needs_human"] = bool(needed)
                row["needs_human_reason"] = reason if needed else None
                row["needs_human_at"] = now if needed else None
                data["machines"] = machines
                save_state(data)
                return jsonify(machine_public_view(_machine_from_dict(row), now))
        return jsonify({"error": "not found"}), 404


def _human_input_machine_exists(mid: str) -> bool:
    with _state_lock:
        data = load_state()
        return any(r.get("id") == mid for r in (data.get("machines") or []))


@app.put("/api/machines/<mid>/human-input/requests/<rid>")
def human_input_put_request(mid: str, rid: str):
    """Agent registers a pending human-input card (short-poll + UI)."""
    if not _human_input_machine_exists(mid):
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    kind = str(body.get("kind") or "field").strip() or "field"
    item = body.get("item")
    if not isinstance(item, dict):
        item = {}
    coll = _human_input_coll()
    now = time.time()
    coll.delete_many({"machine_id": mid, "status": "pending", "_id": {"$ne": rid}})
    doc = {
        "_id": rid,
        "machine_id": mid,
        "status": "pending",
        "created_at": now,
        "kind": kind,
        "item": item,
        "response": None,
        "answered_at": None,
    }
    coll.replace_one({"_id": rid}, doc, upsert=True)
    return jsonify({"ok": True, "request_id": rid})


@app.get("/api/machines/<mid>/human-input/requests/<rid>")
def human_input_get_request(mid: str, rid: str):
    """Agent polls until status becomes answered."""
    coll = _human_input_coll()
    cur = coll.find_one({"_id": rid})
    if not cur or cur.get("machine_id") != mid:
        return jsonify({"error": "not found"}), 404
    out = {k: v for k, v in cur.items() if k != "_id"}
    out["request_id"] = str(cur.get("_id"))
    cr = cur.get("created_at")
    if isinstance(cr, (int, float)) and cur.get("status") == "pending":
        out["poll_hint_interval_s"] = _human_input_poll_hint_s(float(cr))
    return jsonify(out)


@app.delete("/api/machines/<mid>/human-input/requests/<rid>")
def human_input_delete_request(mid: str, rid: str):
    coll = _human_input_coll()
    coll.delete_one({"_id": rid, "machine_id": mid})
    return jsonify({"ok": True})


@app.get("/api/machines/<mid>/human-input/current")
def human_input_current(mid: str):
    """UI polls for the active pending prompt for this machine."""
    if not _human_input_machine_exists(mid):
        return jsonify({"error": "not found"}), 404
    coll = _human_input_coll()
    cur = coll.find_one({"machine_id": mid, "status": "pending"}, sort=[("created_at", -1)])
    if not cur:
        return jsonify({"pending": False})
    cr = cur.get("created_at")
    ph = _human_input_poll_hint_s(float(cr)) if isinstance(cr, (int, float)) else 0.25
    return jsonify(
        {
            "pending": True,
            "request_id": str(cur.get("_id")),
            "created_at": cr,
            "poll_hint_interval_s": ph,
            "kind": cur.get("kind"),
            "item": cur.get("item") if isinstance(cur.get("item"), dict) else {},
        }
    )


@app.post("/api/machines/<mid>/human-input/requests/<rid>/answer")
def human_input_post_answer(mid: str, rid: str):
    """Browser submits an answer for a pending request."""
    coll = _human_input_coll()
    cur = coll.find_one({"_id": rid})
    if not cur or cur.get("machine_id") != mid:
        return jsonify({"error": "not found"}), 404
    if cur.get("status") != "pending":
        return jsonify({"error": "not pending"}), 409
    body = request.get_json(silent=True) or {}
    resp: dict[str, Any] = {}
    for k in ("value", "force_submit", "promote_to_absolute", "confirmed", "continue"):
        if k in body:
            resp[k] = body[k]
    coll.update_one(
        {"_id": rid},
        {"$set": {"status": "answered", "response": resp, "answered_at": time.time()}},
    )
    return jsonify({"ok": True})


@app.post("/api/machines/<mid>/telemetry")
def machine_telemetry(mid: str):
    """
    Lightweight telemetry endpoint used by the agent to progressively report
    token usage, cost, and its own observed control state while a run is
    still in progress.

    Expected JSON (any subset):
      {
        "llm_tokens": 12345,
        "llm_cost_usd": 0.0123,
        "agent_state": "running" | "paused" | "stopping"
      }
    """
    body = request.get_json(silent=True) or {}
    llm_tokens = body.get("llm_tokens")
    llm_cost_usd = body.get("llm_cost_usd")
    agent_state = body.get("agent_state")

    with _state_lock:
        data = load_state()
        machines: list[dict[str, Any]] = list(data.get("machines") or [])
        for row in machines:
            if row.get("id") != mid:
                continue
            changed = False
            if isinstance(llm_tokens, (int, float)):
                row["llm_tokens"] = int(llm_tokens)
                changed = True
            if isinstance(llm_cost_usd, (int, float)):
                row["llm_cost_usd"] = float(llm_cost_usd)
                changed = True
            if isinstance(agent_state, str) and agent_state in ("running", "paused", "stopping"):
                row["agent_state"] = agent_state
                row["agent_state_at"] = time.time()
                changed = True
            if changed:
                data["machines"] = machines
                save_state(data)
            return jsonify({"ok": True, "machine": machine_public_view(_machine_from_dict(row), time.time())})
        return jsonify({"error": "not found"}), 404


@app.post("/api/machines/<mid>/agent-pause")
def set_agent_pause(mid: str):
    """
    Pause or resume the agent via the cooperative control file.

    The agent watches ``state.json`` inside its bind-mounted control directory
    and blocks before every LLM call while ``state`` is ``paused``. This is
    far more reliable than SIGSTOP, which may be missed by ``pgrep`` or fail
    to interrupt in-flight HTTP requests.

    JSON body:
      { "paused": true }   — block the agent before its next LLM call
      { "paused": false }  — resume normal operation
    """
    body = request.get_json(silent=True) or {}
    paused = bool(body.get("paused", True))

    with _state_lock:
        data = load_state()
        machines: list[dict[str, Any]] = list(data.get("machines") or [])
        row = next((r for r in machines if r.get("id") == mid), None)
        if not row:
            return jsonify({"error": "not found"}), 404

        if row.get("status") != "running":
            return jsonify({"error": "machine is not running"}), 400

        # Don't demote a previously-requested takeover to a mere pause. If a
        # takeover is in progress, leave it stopping; only refuse the resume.
        try:
            cur = _read_control_state(mid)
        except Exception:
            cur = "running"
        if cur == "stopping" and paused is False:
            return jsonify({"error": "takeover is in progress; restart the machine to resume"}), 409

        try:
            _write_control_state(mid, state="paused" if paused else "running")
        except OSError as exc:
            return jsonify({"error": f"failed to write control file: {exc}"}), 500

        # Best-effort signal: SIGSTOP as a belt-and-braces freeze *only* when
        # pausing. This is intentionally ignored on failure — the control file
        # is the source of truth, the signal is just a responsiveness boost.
        cid = row.get("container_id")
        if cid and docker_available() and inspect_running(cid) is True:
            if paused:
                _docker_signal_agent_process(str(cid), pause=True)
            else:
                # Always lift any SIGSTOP we may have set previously, so the
                # agent can actually observe the new "running" state file.
                _docker_signal_agent_process(str(cid), pause=False)

        row["agent_paused"] = paused
        # Optimistically reflect the intent. The agent's telemetry will
        # overwrite `agent_state` with the observed state shortly.
        row["agent_state"] = "paused" if paused else "running"
        row["agent_state_at"] = time.time()
        data["machines"] = machines
        save_state(data)
        return jsonify(machine_public_view(_machine_from_dict(row), time.time()))


@app.post("/api/machines/<mid>/takeover")
def set_agent_takeover(mid: str):
    """
    Ask the agent to stop gracefully so the user can take over the desktop.

    Semantics:
      - Writes ``state=stopping`` to the cooperative control file. On its next
        LLM call the agent raises a clean ``SystemExit`` with a dedicated
        status code.
      - Lifts any SIGSTOP the orchestrator may have set previously so the
        agent is actually able to read the control file.
      - Leaves the container running so the VNC desktop and terminal stay
        accessible for the human.
    """
    with _state_lock:
        data = load_state()
        machines: list[dict[str, Any]] = list(data.get("machines") or [])
        row = next((r for r in machines if r.get("id") == mid), None)
        if not row:
            return jsonify({"error": "not found"}), 404

        if row.get("status") != "running":
            return jsonify({"error": "machine is not running"}), 400

        try:
            _write_control_state(mid, state="stopping")
        except OSError as exc:
            return jsonify({"error": f"failed to write control file: {exc}"}), 500

        cid = row.get("container_id")
        if cid and docker_available() and inspect_running(cid) is True:
            # Make sure the agent is not SIGSTOPped — it has to run long
            # enough to observe the state and exit.
            _docker_signal_agent_process(str(cid), pause=False)

        row["agent_paused"] = False
        row["agent_state"] = "stopping"
        row["agent_state_at"] = time.time()
        data["machines"] = machines
        save_state(data)
        return jsonify(machine_public_view(_machine_from_dict(row), time.time()))


def _control_dir_check_path(mid: str) -> Path:
    check, _host = _mountable_paths(_control_dir_rel(mid))
    return check


@app.get("/api/machines/<mid>/user-guidance")
def get_user_guidance(mid: str):
    """Return operator-authored guidance text synced into the agent control directory."""
    with _state_lock:
        data = load_state()
        if not any(r.get("id") == mid for r in (data.get("machines") or [])):
            return jsonify({"error": "not found"}), 404
    p = _control_dir_check_path(mid) / USER_GUIDANCE_FILE
    try:
        if not p.is_file():
            return jsonify({"text": ""})
        return jsonify({"text": p.read_text(encoding="utf-8", errors="replace")})
    except OSError as exc:
        return jsonify({"error": f"failed to read guidance: {exc}"}), 500


@app.put("/api/machines/<mid>/user-guidance")
def put_user_guidance(mid: str):
    """Write guidance the agent reads before each LLM call (see agent/llm_usage.py)."""
    body = request.get_json(silent=True) or {}
    text = body.get("text", "")
    if text is None:
        text = ""
    if not isinstance(text, str):
        return jsonify({"error": "text must be a string"}), 400
    if len(text) > 50_000:
        return jsonify({"error": "text too long (max 50000 chars)"}), 400
    with _state_lock:
        data = load_state()
        if not any(r.get("id") == mid for r in (data.get("machines") or [])):
            return jsonify({"error": "not found"}), 404
    base = _control_dir_check_path(mid)
    try:
        base.mkdir(parents=True, exist_ok=True)
        (base / USER_GUIDANCE_FILE).write_text(text, encoding="utf-8")
    except OSError as exc:
        return jsonify({"error": f"failed to write guidance: {exc}"}), 500
    return jsonify({"ok": True})


@app.post("/api/machines/<mid>/desktop-paste")
def desktop_paste_from_host(mid: str):
    """
    Copy JSON ``text`` into the machine's X11 clipboard and synthesize Ctrl+V
    so the focused window receives the host-provided string (e.g. from the
    orchestrator browser clipboard).
    """
    body = request.get_json(silent=True) or {}
    text = body.get("text")
    if not isinstance(text, str):
        return jsonify({"error": "text must be a string"}), 400
    if len(text) > 512_000:
        return jsonify({"error": "text too long"}), 400
    if not docker_available():
        return jsonify({"error": "Docker is not available"}), 503
    with _state_lock:
        data = load_state()
        row = next((r for r in (data.get("machines") or []) if r.get("id") == mid), None)
    if not row:
        return jsonify({"error": "not found"}), 404
    if row.get("status") != "running":
        return jsonify({"error": "machine is not running"}), 400
    cid = row.get("container_id")
    if not cid:
        return jsonify({"error": "no container"}), 400
    if inspect_running(str(cid)) is not True:
        return jsonify({"error": "container is not running"}), 400

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as tf:
        tf.write(text)
        host_tmp = tf.name
    try:
        r = _docker(["cp", host_tmp, f"{cid}:/tmp/okto-orch-paste.txt"], timeout=30.0)
        if r.returncode != 0:
            err = ((r.stderr or "") + (r.stdout or "")).strip() or "docker cp failed"
            return jsonify({"error": err[:2000]}), 500
        inner = (
            "export DISPLAY=:99; "
            "xclip -selection clipboard < /tmp/okto-orch-paste.txt && "
            "xdotool key ctrl+v; "
            "rm -f /tmp/okto-orch-paste.txt"
        )
        r2 = _docker(["exec", str(cid), "bash", "-lc", inner], timeout=30.0)
        if r2.returncode != 0:
            err = ((r2.stderr or "") + (r2.stdout or "")).strip() or "paste exec failed"
            return jsonify({"error": err[:2000]}), 500
    finally:
        try:
            os.unlink(host_tmp)
        except OSError:
            pass
    return jsonify({"ok": True})


def _read_control_state(mid: str) -> str:
    """Best-effort reader for the current control state ('running' default)."""
    check, _host = _mountable_paths(_control_dir_rel(mid))
    state_path = check / CONTROL_FILE_NAME
    try:
        doc = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "running"
    s = doc.get("state") if isinstance(doc, dict) else None
    if s in ("running", "paused", "stopping"):
        return s
    return "running"


@app.delete("/api/machines/<mid>/remove")
def remove_machine(mid: str):
    if not docker_available():
        return jsonify({"error": "Docker is not available"}), 503

    with _state_lock:
        data = load_state()
        machines: list[dict[str, Any]] = list(data.get("machines") or [])
        idx = next((i for i, r in enumerate(machines) if r.get("id") == mid), None)
        if idx is None:
            return jsonify({"error": "not found"}), 404

        row = machines[idx]
        cid = row.get("container_id")
        if cid:
            _docker(["rm", "-f", str(cid)], timeout=90.0)

        machines.pop(idx)
        data["machines"] = machines
        save_state(data)
        return jsonify({"ok": True})


@app.post("/api/machines/<mid>/applications")
def bump_applications(mid: str):
    """Increment successful application count for a machine (manual / webhook hook)."""
    body = request.get_json(silent=True) or {}
    delta = int(body.get("delta", 1))
    with _state_lock:
        data = load_state()
        machines: list[dict[str, Any]] = list(data.get("machines") or [])
        for row in machines:
            if row.get("id") == mid:
                row["applications_submitted"] = int(row.get("applications_submitted") or 0) + delta
                data["machines"] = machines
                save_state(data)
                return jsonify(machine_public_view(_machine_from_dict(row), time.time()))
        return jsonify({"error": "not found"}), 404


@app.get("/api/machines/<mid>/latest-application")
def latest_application_for_machine(mid: str):
    """
    Return the newest application record recorded by the given machine, if any.
    Used by the MachineCard to decide whether to show the "Submit" review button.
    """
    # Scan up to 2000 recent records; machines are usually short-lived.
    for r in read_application_records(limit=2000):
        if r.get("machine_id") == mid:
            return jsonify(_enrich_application_record_dict(r))
    return jsonify(None)


_APP_PATCHABLE_KEYS = {
    "status",
    "description",
    "reviewed",
    "fields",
    "application_url",
    "job_title",
    "job_company",
    "job_city",
}
_APP_ALLOWED_STATUSES = {"In progress", "Finished", "Not found", "Failed", "Submitted"}


@app.patch("/api/applications/<app_id>")
def patch_application(app_id: str):
    """
    Human-review edits to a finished application record. Supports updating
    `status`, `description`, `fields` (dict) and `reviewed` (bool). When
    `reviewed=true`, also stamps `reviewed_at` + `reviewed_at_iso`.
    """
    body = request.get_json(silent=True) or {}
    patch: dict[str, Any] = {}
    if "status" in body:
        st = str(body.get("status") or "").strip()
        if st and st not in _APP_ALLOWED_STATUSES:
            return jsonify({"error": f"invalid status: {st}"}), 400
        patch["status"] = st or "Failed"
    if "description" in body:
        patch["description"] = str(body.get("description") or "").strip()
    if "application_url" in body:
        patch["application_url"] = str(body.get("application_url") or "").strip()
    if "job_title" in body:
        patch["job_title"] = str(body.get("job_title") or "").strip()
    if "job_company" in body:
        patch["job_company"] = str(body.get("job_company") or "").strip()
    if "job_city" in body:
        patch["job_city"] = str(body.get("job_city") or "").strip()
    if "fields" in body:
        f = body.get("fields")
        if not isinstance(f, dict):
            return jsonify({"error": "fields must be an object"}), 400
        patch["fields"] = f
    if "reviewed" in body:
        r = bool(body.get("reviewed"))
        patch["reviewed"] = r
        if r:
            now = time.time()
            patch["reviewed_at"] = now
            patch["reviewed_at_iso"] = _utc_iso(now)
    if not patch:
        return jsonify({"error": "no supported fields in patch"}), 400
    updated = update_application_record(app_id, patch)
    if updated is None:
        return jsonify({"error": "application not found"}), 404
    return jsonify(_enrich_application_record_dict(updated))


@app.get("/api/applications")
def list_applications():
    """
    Return past application records (newest first).
    Query params:
      - limit: max records (default 200, max 2000)
    """
    try:
        limit = int(request.args.get("limit") or 200)
    except Exception:
        limit = 200
    limit = max(1, min(limit, 2000))
    rows = read_application_records(limit=limit)
    return jsonify([_enrich_application_record_dict(x) for x in rows])


@app.post("/api/llm-ledger/append")
def append_llm_ledger_row():
    """
    Append a single LLM usage ledger row (JSON object) into Mongo.

    The agent calls this endpoint when AGENT_LLM_LEDGER_URL is set.
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "expected a JSON object"}), 400
    doc = dict(body)
    # Add a server timestamp (seconds) for sorting even if caller omitted it.
    if "ts_unix" not in doc:
        doc["ts_unix"] = time.time()
    try:
        _llm_ledger_coll().insert_one(doc)
        return jsonify({"ok": True})
    except PyMongoError:
        return jsonify({"error": "Failed to append ledger row"}), 500


@app.get("/api/profiles")
def list_profiles():
    """
    List profile ids.
    """
    try:
        _maybe_import_profiles_json_to_mongo()
        coll = _profiles_coll()
        cur = coll.find({}, {"profile_id": 1, "profile.label": 1})
        out: list[dict[str, Any]] = []
        for doc in cur:
            pid = doc.get("profile_id")
            if not isinstance(pid, str):
                continue
            prof = doc.get("profile") if isinstance(doc.get("profile"), dict) else {}
            label = prof.get("label") if isinstance(prof, dict) else None
            out.append({"profile_id": pid, "label": label or None})
        out.sort(key=lambda x: (x.get("profile_id") or ""))
        return jsonify({"profiles": out})
    except PyMongoError:
        return jsonify({"error": "Failed to query profiles store"}), 500


@app.get("/api/profiles/<profile_id>")
def get_profile(profile_id: str):
    profile_id = (profile_id or "").strip()
    if not profile_id:
        return jsonify({"error": "profile_id required"}), 400
    try:
        _maybe_import_profiles_json_to_mongo()
        coll = _profiles_coll()
        doc = coll.find_one({"profile_id": profile_id}, {"profile": 1})
        prof = doc.get("profile") if isinstance(doc, dict) else None
    except PyMongoError:
        return jsonify({"error": "Failed to read profiles store"}), 500
    if not isinstance(prof, dict):
        return jsonify({"error": "not found"}), 404
    return jsonify({"profile_id": profile_id, "profile": prof})


@app.put("/api/profiles/<profile_id>")
def put_profile(profile_id: str):
    """
    Create or replace a profile.
    Body:
      { "profile": { ... } }
    """
    profile_id = (profile_id or "").strip()
    body = request.get_json(silent=True) or {}
    if not profile_id:
        return jsonify({"error": "profile_id required"}), 400
    prof = body.get("profile")
    if not isinstance(prof, dict):
        return jsonify({"error": "body.profile must be an object"}), 400
    prof = dict(prof)
    # Keep ids consistent.
    prof["profile_id"] = profile_id
    try:
        coll = _profiles_coll()
        coll.replace_one(
            {"profile_id": profile_id},
            {"profile_id": profile_id, "profile": prof},
            upsert=True,
        )
    except PyMongoError:
        return jsonify({"error": "Failed to write profiles store"}), 500
    return jsonify({"ok": True, "profile_id": profile_id, "profile": prof})


@app.delete("/api/profiles/<profile_id>")
def delete_profile(profile_id: str):
    profile_id = (profile_id or "").strip()
    if not profile_id:
        return jsonify({"error": "profile_id required"}), 400
    try:
        coll = _profiles_coll()
        r = coll.delete_one({"profile_id": profile_id})
        if int(getattr(r, "deleted_count", 0) or 0) <= 0:
            return jsonify({"error": "not found"}), 404
    except PyMongoError:
        return jsonify({"error": "Failed to write profiles store"}), 500
    return jsonify({"ok": True})


@app.get("/api/profiles/<profile_id>/attachments")
def list_attachments(profile_id: str):
    """
    List uploaded documents for a profile.

    Returns: { "attachments": [ {name, filename, path, size, mime, uploaded_at, exists}, ... ] }
    """
    profile_id = (profile_id or "").strip()
    if not profile_id:
        return jsonify({"error": "profile_id required"}), 400
    try:
        coll = _profiles_coll()
        doc = coll.find_one({"profile_id": profile_id}, {"profile": 1})
        prof = doc.get("profile") if isinstance(doc, dict) else None
        if not isinstance(prof, dict):
            return jsonify({"error": "not found"}), 404
        att = prof.get("attachments")
        if not isinstance(att, dict):
            att = {}
        rows = [_attachment_record(profile_id, name, str(path)) for name, path in att.items() if isinstance(path, str)]
        rows.sort(key=lambda r: (r.get("name") or "").lower())
        return jsonify({"profile_id": profile_id, "attachments": rows})
    except PyMongoError:
        return jsonify({"error": "Failed to read profiles store"}), 500


@app.post("/api/profiles/<profile_id>/attachments")
def upload_attachment(profile_id: str):
    """
    Upload a new attachment for a profile.

    multipart/form-data:
      file: <required, the file>
      name: <optional display name; defaults to filename without extension>

    Behavior:
      - Files land at  attachments/<profile_id>/<sanitized-filename>
      - On-disk filename gets a numeric suffix on collision (never overwrite)
      - Display name gets a numeric suffix on collision (keys must be unique)
    """
    profile_id = (profile_id or "").strip()
    if not profile_id:
        return jsonify({"error": "profile_id required"}), 400

    f = request.files.get("file")
    if f is None or not getattr(f, "filename", None):
        return jsonify({"error": "file is required"}), 400

    desired_name = (request.form.get("name") or "").strip()

    try:
        coll = _profiles_coll()
        doc = coll.find_one({"profile_id": profile_id}, {"profile": 1})
        prof = doc.get("profile") if isinstance(doc, dict) else None
        if not isinstance(prof, dict):
            return jsonify({"error": "not found"}), 404
        prof = dict(prof)
        att = _ensure_attachments_map(prof)

        target_dir = _profile_attachments_dir(profile_id)
        original_name = _safe_path_segment(f.filename) or "upload"
        on_disk_name = _unique_disk_filename(target_dir, original_name)
        target_path = target_dir / on_disk_name

        # Stream-save (Werkzeug FileStorage exposes .save() but we want to be defensive
        # about the parent dir already existing).
        f.save(str(target_path))

        # Default display name = filename without extension if user left it blank.
        if not desired_name:
            stem, dot, _ext = original_name.partition(".")
            desired_name = (stem if dot else original_name) or "Attachment"
        display_name = _unique_display_name(att, desired_name)

        repo_rel = f"{_ATTACHMENTS_REPO_DIR}/{_safe_path_segment(profile_id)}/{on_disk_name}"
        att[display_name] = repo_rel

        try:
            _persist_profile(coll, profile_id, prof)
        except PyMongoError:
            # Roll back the on-disk write so we don't get an orphan file.
            try:
                target_path.unlink()
            except OSError:
                pass
            return jsonify({"error": "Failed to write profiles store"}), 500

        return jsonify({
            "ok": True,
            "profile_id": profile_id,
            "attachment": _attachment_record(profile_id, display_name, repo_rel),
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        return jsonify({"error": f"Failed to save file: {e}"}), 500
    except PyMongoError:
        return jsonify({"error": "Failed to read profiles store"}), 500


@app.get("/api/profiles/<profile_id>/attachments/<path:name>/download")
def download_attachment(profile_id: str, name: str):
    """Stream the file for `name` back to the client with a Content-Disposition."""
    profile_id = (profile_id or "").strip()
    name = (name or "").strip()
    if not profile_id or not name:
        return jsonify({"error": "profile_id and name required"}), 400
    try:
        coll = _profiles_coll()
        doc = coll.find_one({"profile_id": profile_id}, {"profile": 1})
        prof = doc.get("profile") if isinstance(doc, dict) else None
        if not isinstance(prof, dict):
            return jsonify({"error": "not found"}), 404
        att = prof.get("attachments")
        if not isinstance(att, dict) or name not in att:
            return jsonify({"error": "attachment not found"}), 404
        rel = str(att.get(name) or "").strip()
    except PyMongoError:
        return jsonify({"error": "Failed to read profiles store"}), 500

    # Sanity: stored path must stay under attachments/.
    if not rel or ".." in rel.split("/") or not rel.startswith(f"{_ATTACHMENTS_REPO_DIR}/"):
        return jsonify({"error": "invalid attachment path"}), 500

    target = (REPO_ROOT / rel).resolve()
    try:
        target.relative_to(_attachments_root_local().resolve())
    except ValueError:
        return jsonify({"error": "invalid attachment path"}), 500
    if not target.is_file():
        return jsonify({"error": "file missing on disk"}), 410

    return send_from_directory(
        target.parent,
        target.name,
        as_attachment=True,
        download_name=target.name,
    )


@app.delete("/api/profiles/<profile_id>/attachments/<path:name>")
def delete_attachment(profile_id: str, name: str):
    """Hard-delete: remove the file from disk AND the entry from the profile JSON."""
    profile_id = (profile_id or "").strip()
    name = (name or "").strip()
    if not profile_id or not name:
        return jsonify({"error": "profile_id and name required"}), 400
    try:
        coll = _profiles_coll()
        doc = coll.find_one({"profile_id": profile_id}, {"profile": 1})
        prof = doc.get("profile") if isinstance(doc, dict) else None
        if not isinstance(prof, dict):
            return jsonify({"error": "not found"}), 404
        prof = dict(prof)
        att = _ensure_attachments_map(prof)
        if name not in att:
            return jsonify({"error": "attachment not found"}), 404
        rel = str(att.pop(name) or "").strip()

        # Best-effort file delete: we keep the JSON change even if the file is
        # already gone (so the table doesn't show a phantom row).
        if rel and rel.startswith(f"{_ATTACHMENTS_REPO_DIR}/") and ".." not in rel.split("/"):
            target = (REPO_ROOT / rel).resolve()
            try:
                target.relative_to(_attachments_root_local().resolve())
                if target.is_file():
                    target.unlink()
            except (ValueError, OSError):
                pass

        _persist_profile(coll, profile_id, prof)
        return jsonify({"ok": True, "profile_id": profile_id, "name": name})
    except PyMongoError:
        return jsonify({"error": "Failed to write profiles store"}), 500


@app.post("/api/profiles/<profile_id>/custom-fields")
def patch_custom_fields(profile_id: str):
    """
    Field-level editor for profile.other.custom.relative_fields / absolute_fields.

    Body supports ONE operation per request:
      - set:    { "op": "set", "scope": "relative"|"absolute", "key": "...", "value": "..." }
      - delete: { "op": "delete", "scope": "relative"|"absolute", "key": "..." }
      - promote:{ "op": "promote", "key": "...", "overwrite": false }
                 moves key from relative_fields -> absolute_fields
    """
    profile_id = (profile_id or "").strip()
    body = request.get_json(silent=True) or {}
    op = str(body.get("op") or "").strip().lower()
    if not profile_id:
        return jsonify({"error": "profile_id required"}), 400
    if op not in {"set", "delete", "promote"}:
        return jsonify({"error": "op must be set, delete, or promote"}), 400
    key = str(body.get("key") or "").strip()
    if not key:
        return jsonify({"error": "key required"}), 400

    scope = str(body.get("scope") or "").strip().lower()
    if op in {"set", "delete"} and scope not in {"relative", "absolute"}:
        return jsonify({"error": "scope must be relative or absolute"}), 400

    overwrite = bool(body.get("overwrite", False))
    value = body.get("value")
    if op == "set" and value is None:
        # allow empty string, but not missing
        return jsonify({"error": "value required for set"}), 400

    try:
        coll = _profiles_coll()
        doc = coll.find_one({"profile_id": profile_id}, {"profile": 1})
        prof = doc.get("profile") if isinstance(doc, dict) else None
        if not isinstance(prof, dict):
            return jsonify({"error": "not found"}), 404
        prof = dict(prof)
        rel, absf = _ensure_custom_maps(prof)

        if op == "set":
            target = rel if scope == "relative" else absf
            target[key] = value
        elif op == "delete":
            target = rel if scope == "relative" else absf
            target.pop(key, None)
        else:
            if key not in rel:
                return jsonify({"error": "key not found in relative_fields"}), 404
            if (not overwrite) and key in absf:
                return jsonify({"error": "key already exists in absolute_fields (set overwrite=true to replace)"}), 409
            absf[key] = rel[key]
            rel.pop(key, None)

        coll.replace_one(
            {"profile_id": profile_id},
            {"profile_id": profile_id, "profile": prof},
            upsert=False,
        )
        return jsonify({"ok": True, "profile_id": profile_id, "profile": prof})
    except PyMongoError:
        return jsonify({"error": "Failed to write profiles store"}), 500


# ---------------------------------------------------------------------------
# Per-step screenshots captured by the agent and served as a carousel on the
# applications page.
# ---------------------------------------------------------------------------


def _run_dir(run_id: str) -> Path | None:
    if not _RUN_ID_RE.match(run_id or ""):
        return None
    return SCREENSHOTS_DIR / run_id


def _list_run_shots(run_id: str) -> list[dict[str, Any]]:
    d = _run_dir(run_id)
    if not d or not d.is_dir():
        return []
    meta_path = d / "meta.jsonl"
    metas: dict[int, dict[str, Any]] = {}
    if meta_path.is_file():
        try:
            for line in meta_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if isinstance(entry, dict) and isinstance(entry.get("index"), int):
                    metas[int(entry["index"])] = entry
        except Exception:
            metas = {}
    shots: list[dict[str, Any]] = []
    for f in sorted(d.glob("*.png")):
        try:
            idx = int(f.stem)
        except Exception:
            continue
        meta = metas.get(idx) or {}
        # Use the exact on-disk filename (e.g. 0028.png) so GET resolves the file
        # even though earlier code advertised unpadded URLs like 28.png.
        shots.append(
            {
                "index": idx,
                "url": f"/api/screenshots/{run_id}/{f.name}",
                "page_url": meta.get("page_url") or "",
                "next_goal": meta.get("next_goal") or "",
                "field": meta.get("field") or "",
                "captured_at": meta.get("captured_at"),
            }
        )
    shots.sort(key=lambda x: x["index"])
    return shots


def _enrich_application_record_dict(record: dict[str, Any]) -> dict[str, Any]:
    """
    Attach on-disk step screenshots when `run_id` is set. Placeholder records are
    updated with `run_id` on first screenshot POST so the review dialog can show
    progress before the agent posts a final application-result.
    """
    if not isinstance(record, dict):
        return record
    out = dict(record)
    rid = str(out.get("run_id") or "").strip()
    if rid and _RUN_ID_RE.match(rid):
        out["screenshots"] = _list_run_shots(rid)
        out["screenshot_count"] = len(out["screenshots"])
    return out


@app.post("/api/machines/<mid>/screenshot")
def log_step_screenshot(mid: str):
    """
    Called by the agent container after each step to save a full-page screenshot.

    Expected JSON body:
      {
        "run_id": "<uuid>",            # groups screenshots for a single application run
        "step_index": 0,                # monotonically increasing
        "image_b64": "<base64 png>",    # may include 'data:image/png;base64,' prefix
        "page_url": "https://...",      # optional
        "next_goal": "...",             # optional
      }
    """
    body = request.get_json(silent=True) or {}
    run_id = str(body.get("run_id") or "").strip()
    d = _run_dir(run_id)
    if d is None:
        return jsonify({"error": "invalid run_id"}), 400

    try:
        step_index = int(body.get("step_index"))
    except (TypeError, ValueError):
        return jsonify({"error": "step_index required"}), 400
    if step_index < 0 or step_index >= SCREENSHOT_MAX_PER_RUN:
        return jsonify({"error": "step_index out of range"}), 400

    raw = body.get("image_b64")
    if not isinstance(raw, str) or not raw:
        return jsonify({"error": "image_b64 required"}), 400
    if raw.startswith("data:"):
        comma = raw.find(",")
        raw = raw[comma + 1 :] if comma >= 0 else ""
    try:
        payload = base64.b64decode(raw, validate=False)
    except Exception:
        return jsonify({"error": "image_b64 is not valid base64"}), 400
    if len(payload) == 0:
        return jsonify({"error": "empty image"}), 400
    if len(payload) > SCREENSHOT_MAX_BYTES:
        return jsonify({"error": "image too large"}), 413

    d.mkdir(parents=True, exist_ok=True)
    png_path = d / f"{step_index:04d}.png"
    try:
        png_path.write_bytes(payload)
    except Exception as e:
        return jsonify({"error": f"failed to write screenshot: {e}"}), 500

    meta_entry = {
        "index": step_index,
        "machine_id": mid,
        "page_url": (body.get("page_url") or "").strip(),
        "next_goal": (body.get("next_goal") or "").strip(),
        "field": (body.get("field") or "").strip(),
        "captured_at": _utc_iso(),
    }
    try:
        with (d / "meta.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(meta_entry, ensure_ascii=False) + "\n")
    except Exception:
        # Metadata is best-effort; the png is still usable.
        pass
    try:
        coll = _applications_coll()
        cur = coll.find(
            {
                "machine_id": mid,
                "status": "In progress",
                "$or": [
                    {"run_id": {"$exists": False}},
                    {"run_id": None},
                    {"run_id": ""},
                ],
            },
            {"id": 1},
        ).sort("ts", -1).limit(1)
        doc = next(cur, None)
        if isinstance(doc, dict) and doc.get("id"):
            coll.update_one({"id": doc["id"]}, {"$set": {"run_id": run_id}})
    except PyMongoError:
        pass
    return jsonify({"ok": True, "bytes": len(payload), "index": step_index}), 201


@app.get("/api/screenshots/<run_id>/manifest")
def screenshots_manifest(run_id: str):
    if not _RUN_ID_RE.match(run_id or ""):
        return jsonify({"error": "invalid run_id"}), 400
    return jsonify({"run_id": run_id, "shots": _list_run_shots(run_id)})


@app.get("/api/screenshots/<run_id>/<path:filename>")
def screenshots_file(run_id: str, filename: str):
    d = _run_dir(run_id)
    if d is None or not d.is_dir():
        abort(404)
    # Only allow simple "NNNN.png" names. No traversal, no nested paths.
    if "/" in filename or "\\" in filename or ".." in filename:
        abort(404)
    if not filename.lower().endswith(".png"):
        abort(404)
    # Tolerate unpadded requests like "28.png" when on disk the file is "0028.png".
    requested = d / filename
    if not requested.is_file():
        stem = filename[:-4]
        try:
            idx = int(stem)
            padded = f"{idx:04d}.png"
            if (d / padded).is_file():
                filename = padded
        except ValueError:
            pass
    return send_from_directory(d, filename, mimetype="image/png", max_age=3600)


@app.post("/api/machines/<mid>/application-result")
def log_application_result(mid: str):
    """
    Called by the agent container to persist an application attempt result.

    Expected JSON (flexible, extra keys allowed):
      {
        "application_url": "https://...",
        "status": "Finished" | "Not found" | "Failed",
        "description": "...",
        "fields": { "base.email": "...", ... }
      }
    """
    body = request.get_json(silent=True) or {}
    now = time.time()
    # Best-effort: derive per-application duration + cost from the machine session.
    duration_seconds: float | None = None
    duration_label: str | None = None
    runtime_cost_usd: float | None = None
    machine_row: dict[str, Any] | None = None
    try:
        with _state_lock:
            data = load_state()
            machines: list[dict[str, Any]] = list(data.get("machines") or [])
            machine_row = next((r for r in machines if r.get("id") == mid), None)
        if machine_row and machine_row.get("started_at"):
            started_at = float(machine_row["started_at"])
            duration_seconds = round(max(0.0, now - started_at), 1)
            duration_label = _fmt_uptime(duration_seconds)
            runtime_cost_usd = round(max(0.0, duration_seconds) / 3600.0 * COST_PER_HOUR_USD, 4)
    except Exception:
        # Don't fail logging if state is unavailable/unexpected.
        duration_seconds = None
        duration_label = None
        runtime_cost_usd = None
        machine_row = None

    def _pick_machine_str(key: str, fallback: str = "") -> str:
        v = (body.get(key) or "").strip() if isinstance(body.get(key), str) else ""
        if v:
            return v
        if machine_row:
            w = machine_row.get(key)
            if isinstance(w, str) and w.strip():
                return w.strip()
        return fallback

    profile_id = _pick_machine_str("profile_id")
    llm_model = _pick_machine_str("llm_model")
    application_url = (body.get("application_url") or "").strip()
    if not application_url and machine_row:
        application_url = (machine_row.get("job_url") or "").strip()

    # Enrich with source-api metadata so the applications page can show
    # human-friendly title/company/city instead of a raw URL.
    job_title = (body.get("job_title") or "").strip()
    job_company = (body.get("job_company") or "").strip()
    job_city = (body.get("job_city") or "").strip()
    if machine_row:
        if not job_title:
            job_title = (machine_row.get("job_title") or "").strip()
        if not job_company:
            job_company = (machine_row.get("job_company") or "").strip()
        if not job_city:
            job_city = (machine_row.get("job_city") or "").strip()
    if application_url and (not job_title or not job_company):
        try:
            data_for_settings = load_state()
            settings = _get_settings(data_for_settings)
            src_base = settings.get("source_api") or SOURCE_API
            row_job = _src_find_job_by_url(application_url, base=src_base) if src_base else None
            if isinstance(row_job, dict):
                job_title = job_title or (row_job.get("title") or "").strip()
                job_company = job_company or (row_job.get("company") or "").strip()
                job_city = job_city or (row_job.get("city") or "").strip()
        except Exception:
            pass

    llm_cost_usd: float | None = None
    try:
        v = body.get("llm_cost_usd")
        if isinstance(v, (int, float)):
            llm_cost_usd = float(v)
    except Exception:
        llm_cost_usd = None

    llm_tokens: int | None = None
    try:
        t = body.get("llm_tokens")
        if isinstance(t, (int, float)):
            llm_tokens = int(t)
    except Exception:
        llm_tokens = None

    # Preserve legacy `cost_usd` while switching to token-based cost when available.
    # If the agent sends llm_cost_usd, prefer it; otherwise fall back to runtime-based estimate.
    cost_usd: float | None = llm_cost_usd if llm_cost_usd is not None else runtime_cost_usd
    run_id = str(body.get("run_id") or "").strip()
    shots = _list_run_shots(run_id) if run_id and _RUN_ID_RE.match(run_id) else []
    record = {
        "id": str(uuid.uuid4()),
        "machine_id": mid,
        "run_id": run_id or None,
        "created_at": now,
        "created_at_iso": _utc_iso(now),
        "application_url": application_url,
        "job_title": job_title,
        "job_company": job_company,
        "job_city": job_city,
        "profile_id": profile_id,
        "llm_model": llm_model,
        "status": (body.get("status") or "").strip() or "Failed",
        "description": (body.get("description") or "").strip(),
        "fields": body.get("fields") if isinstance(body.get("fields"), dict) else {},
        "duration_seconds": duration_seconds,
        "duration_label": duration_label,
        "cost_usd": cost_usd,
        "runtime_cost_usd": runtime_cost_usd,
        "llm_cost_usd": llm_cost_usd,
        "llm_tokens": llm_tokens,
        "screenshots": shots,
        "screenshot_count": len(shots),
        "reviewed": False,
        # Keep raw payload for forward-compat (but cap size in-memory by not expanding it).
        "raw": body,
    }
    append_application_record(record)
    return jsonify({"ok": True, "record": record}), 201


def create_app() -> Flask:
    return app


if __name__ == "__main__":
    ensure_background_sync()
    port = int(os.environ.get("ORCH_PORT", "5050"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
