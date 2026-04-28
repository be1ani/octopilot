from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import inspect
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from profiles.profiler import (
    AskUserMissingInfoParams,
    FieldRequest,
    FieldUiSpec,
    Profiler,
    ResolveDocumentsParams,
    ResolveFieldsParams,
)
from .agent_control import TakeoverRequested, get_default as _get_agent_control
from .llm_usage import LLMUsageRecorder, TokenTrackingLLM

# Enable line editing (arrow keys, history) during interactive prompts.
# Without this, `input()` echoes raw escape sequences like "^[[C" into the value
# and can corrupt answers (e.g. salary "72000" became "720\x1b[C00" in log3.txt).
#
# readline's default keymap leaves a number of terminal keys unbound, so when
# the user presses them the terminal echoes the raw CSI sequence into the line
# buffer ("Verfügbar ab" prompt in log4.txt showed `^[[2~[K[K[K[K...`). We
# bind the common offenders (Insert/Delete/Home/End/PgUp/PgDn/Ctrl-arrows)
# explicitly to sane readline commands so the escape never reaches the buffer.
try:
    import readline  # noqa: F401  (import side effect: enables line editing)

    # If we're on a dumb or missing TERM, readline won't process bindings;
    # bump it to something standard so arrow/edit keys work.
    if (os.getenv("TERM") or "").strip().lower() in ("", "dumb", "unknown"):
        os.environ["TERM"] = "xterm-256color"

    for _binding in (
        "set editing-mode emacs",
        "set enable-bracketed-paste off",
        # Common navigation / editing keys that otherwise echo as raw ESC seqs.
        r'"\e[H":     beginning-of-line',           # Home
        r'"\e[F":     end-of-line',                 # End
        r'"\eOH":     beginning-of-line',           # Home (application mode)
        r'"\eOF":     end-of-line',                 # End  (application mode)
        r'"\e[1~":    beginning-of-line',           # Home (linux console)
        r'"\e[4~":    end-of-line',                 # End  (linux console)
        r'"\e[3~":    delete-char',                 # Delete
        r'"\e[2~":    overwrite-mode',              # Insert (toggle, keeps the ESC out of input)
        r'"\e[5~":    beginning-of-history',        # PgUp
        r'"\e[6~":    end-of-history',              # PgDn
        r'"\e[1;5C":  forward-word',                # Ctrl+Right
        r'"\e[1;5D":  backward-word',               # Ctrl+Left
        r'"\e[1;3C":  forward-word',                # Alt+Right
        r'"\e[1;3D":  backward-word',               # Alt+Left
    ):
        try:
            readline.parse_and_bind(_binding)
        except Exception:
            pass
except Exception:
    pass


# Strip any remaining ANSI CSI/OSC escape sequences that might have leaked into
# an input (e.g. when running without a real TTY or through a pseudo-terminal).
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)")


def _sanitize_user_input(s: str) -> str:
    if not s:
        return s
    s = _ANSI_OSC_RE.sub("", s)
    s = _ANSI_CSI_RE.sub("", s)
    # Drop any lingering raw ESC characters and other C0 control chars (except tab/newline).
    s = "".join(ch for ch in s if ch == "\t" or ch == "\n" or ord(ch) >= 0x20)
    return s


def _load_dotenv(path: str | Path = ".env") -> None:
    """
    Minimal .env loader (KEY=VALUE lines).
    Only sets values that are not already present in os.environ.
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _get_primary_monitor_geometry() -> tuple[int, int, int, int]:
    """
    Best-effort primary monitor geometry (x, y, width, height).

    If AGENT_VIEW_WIDTH and AGENT_VIEW_HEIGHT are set (e.g. Docker/Xvfb), they override
    screen detection so the browser window matches the virtual display.
    Otherwise uses screeninfo, then (0, 0, 1920, 1200).
    """
    ew_raw = (os.getenv("AGENT_VIEW_WIDTH") or "").strip()
    eh_raw = (os.getenv("AGENT_VIEW_HEIGHT") or "").strip()
    if ew_raw.isdigit() and eh_raw.isdigit():
        ew, eh = int(ew_raw), int(eh_raw)
        if ew > 0 and eh > 0:
            return 0, 0, ew, eh
    try:
        from screeninfo import get_monitors  # type: ignore

        mons = get_monitors()
        if mons:
            # Prefer the primary monitor if available, else first.
            m = next((mm for mm in mons if getattr(mm, "is_primary", False)), mons[0])
            x = int(getattr(m, "x", 0) or 0)
            y = int(getattr(m, "y", 0) or 0)
            w = int(getattr(m, "width", 0) or 0)
            h = int(getattr(m, "height", 0) or 0)
            if w > 0 and h > 0:
                return x, y, w, h
    except Exception:
        pass
    return 0, 0, 1920, 1200


_CAPTCHA_HINTS = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "g-recaptcha",
    "gstatic.com/recaptcha",
    "challenges.cloudflare.com",
    "turnstile",
    "cf-challenge",
    "challenge-platform",
    "i am not a robot",
    "i'm not a robot",
    "verify you are human",
    "human verification",
    "are you human",
    "security check",
    "bot detection",
)


def _looks_like_captcha(text: str) -> bool:
    t = (text or "").lower()
    return any(h in t for h in _CAPTCHA_HINTS)


def _browser_state_suggests_captcha(state: Any) -> bool:
    if _looks_like_captcha(getattr(state, "title", "") or "") or _looks_like_captcha(getattr(state, "url", "") or ""):
        return True
    try:
        dom_text = state.dom_state.llm_representation()
        if _looks_like_captcha(dom_text):
            return True
    except Exception:
        pass
    for tab in getattr(state, "tabs", None) or []:
        u = getattr(tab, "url", "") or ""
        ti = getattr(tab, "title", "") or ""
        if _looks_like_captcha(u) or _looks_like_captcha(ti):
            return True
    return False


def _stable_submit_key(action_description: str, page_url: str | None) -> str:
    """
    Make a stable key so we don't re-prompt confirmation due to minor text changes
    between retries (e.g. step numbers, timestamps, punctuation).
    """
    desc = (action_description or "").lower().strip()
    # Remove numbers and repeated whitespace/punctuation noise
    desc = re.sub(r"\d+", "", desc)
    desc = re.sub(r"\s+", " ", desc).strip()
    # Keep only a small safe subset of chars
    desc = re.sub(r"[^a-z\s/_-]+", "", desc).strip()

    url = (page_url or "").strip()
    # Drop query/fragment so it's stable across tracking params
    url = url.split("#", 1)[0].split("?", 1)[0]

    # Coarse bucket for all submit/apply/continue actions
    if any(w in desc for w in ("submit", "apply", "send", "continue", "next", "finish", "complete")):
        bucket = "submit_like"
    else:
        bucket = "action"

    return f"{bucket}@{url}@{desc[:60] or 'unknown'}"


def _normalize_deepseek_model(env_value: str | None) -> str:
    """
    Map DEEPSEEK_MODEL to a DeepSeek API id. Default is deepseek-v4-flash (V4-Flash;
    see https://api-docs.deepseek.com/quick_start/pricing). Legacy deepseek-chat /
    deepseek-reasoner are still accepted.
    """
    raw = (env_value or "").strip()
    if not raw:
        return "deepseek-v4-flash"
    key = raw.lower()
    if key in ("deepseek-v3", "v3", "chat"):
        return "deepseek-chat"
    if key in ("deepseek-r1", "r1", "reasoner"):
        return "deepseek-reasoner"
    if key in ("v4", "v4-flash", "v4f"):
        return "deepseek-v4-flash"
    if key in ("v4-pro", "v4p"):
        return "deepseek-v4-pro"
    return raw


def _make_agent_llm() -> Any:
    """
    LLM for the browser agent. Default is Google Gemini Flash (pay-as-you-go).

    AGENT_LLM_PROVIDER (default: google):
      google      — GOOGLE_API_KEY or GEMINI_API_KEY, GOOGLE_MODEL (default gemini-flash-latest).
                    Optional: GEMINI_NATIVE_JSON_SCHEMA=true to use Gemini native JSON schema (can 400 on complex tools).
      openai      — OPENAI_API_KEY, OPENAI_MODEL (default gpt-4.1; set gpt-4.1-mini to cut cost)
      anthropic   — ANTHROPIC_API_KEY, ANTHROPIC_MODEL (default claude-opus-4-6)
      deepseek    — DEEPSEEK_API_KEY, DEEPSEEK_MODEL (default deepseek-v4-flash; shorthands v4, v4-flash, v4-pro; legacy
                    deepseek-chat / deepseek-reasoner). Optional: DEEPSEEK_BASE_URL (defaults to https://api.deepseek.com/v1;
                    append /beta for the prefix-completion preview endpoint).
      browser_use — BROWSER_USE_API_KEY, BROWSER_USE_MODEL (default bu-2-0 / BU 2.0; alias bu-2)
    """
    provider = (os.getenv("AGENT_LLM_PROVIDER") or "google").strip().lower()

    if provider == "openai":
        from browser_use import ChatOpenAI

        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise SystemExit(
                "OPENAI_API_KEY is not set (AGENT_LLM_PROVIDER=openai).\n"
                "Default is Google Gemini Flash; use GOOGLE_API_KEY or set another provider, e.g.:\n"
                "  AGENT_LLM_PROVIDER=anthropic  + ANTHROPIC_API_KEY\n"
                "  AGENT_LLM_PROVIDER=browser_use + BROWSER_USE_API_KEY"
            )
        model = os.getenv("OPENAI_MODEL", "gpt-4.1")
        # Some OpenAI models (e.g. gpt-5.4-mini) append text after the JSON object; browser-use's
        # strict parse then fails with "trailing characters". SanitizingChatOpenAI trims to the
        # first complete JSON value. Set AGENT_OPENAI_JSON_SANITIZE=0 to use stock ChatOpenAI.
        sanitize = (os.getenv("AGENT_OPENAI_JSON_SANITIZE") or "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )
        if sanitize:
            from agent.sanitizing_chat_openai import SanitizingChatOpenAI

            return SanitizingChatOpenAI(model=model, api_key=api_key)
        return ChatOpenAI(model=model, api_key=api_key)

    if provider in ("anthropic", "claude"):
        from browser_use import ChatAnthropic

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise SystemExit(
                "ANTHROPIC_API_KEY is not set (required when AGENT_LLM_PROVIDER=anthropic)."
            )
        return ChatAnthropic(
            model=os.getenv("ANTHROPIC_MODEL", "claude-opus-4-6"),
            api_key=api_key,
        )

    if provider in ("google", "gemini"):
        from browser_use import ChatGoogle

        api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise SystemExit(
                "GOOGLE_API_KEY or GEMINI_API_KEY is not set (default provider is google / Gemini Flash).\n"
                "Set a key from Google AI Studio, or use e.g. AGENT_LLM_PROVIDER=openai with OPENAI_API_KEY."
            )
        # Native response_schema often 400s on complex tool unions (Gemini rejects optimized JSON Schema:
        # e.g. required[] vs properties mismatch). Prompt-appended JSON mode is slower but reliable.
        use_native_schema = os.getenv("GEMINI_NATIVE_JSON_SCHEMA", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        return ChatGoogle(
            model=os.getenv("GOOGLE_MODEL", "gemini-flash-latest"),
            api_key=api_key,
            supports_structured_output=use_native_schema,
        )

    if provider in ("browser_use", "browser-use", "bu"):
        from browser_use import ChatBrowserUse

        raw = (os.getenv("BROWSER_USE_MODEL") or "bu-2-0").strip()
        # ChatBrowserUse accepts bu-2-0 (BU 2.0); allow shorthand.
        if raw in ("bu-2", "bu2"):
            raw = "bu-2-0"
        return ChatBrowserUse(
            model=raw,
            api_key=os.getenv("BROWSER_USE_API_KEY"),
        )

    if provider == "deepseek":
        # DeepSeek exposes an OpenAI-compatible /chat/completions endpoint.
        # Octopilot uses a thin subclass: `deepseek-reasoner` rejects OpenAI's forced
        # `tool_choice` (see agent/compat_chat_deepseek.py).
        from agent.compat_chat_deepseek import OctopilotChatDeepSeek

        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise SystemExit(
                "DEEPSEEK_API_KEY is not set (required when AGENT_LLM_PROVIDER=deepseek).\n"
                "Get a key from https://platform.deepseek.com/ → API keys."
            )
        raw = _normalize_deepseek_model(os.getenv("DEEPSEEK_MODEL"))
        kwargs: dict[str, Any] = {"model": raw, "api_key": api_key}
        base_url = (os.getenv("DEEPSEEK_BASE_URL") or "").strip()
        if base_url:
            kwargs["base_url"] = base_url
        return OctopilotChatDeepSeek(**kwargs)

    raise SystemExit(
        f"Unknown AGENT_LLM_PROVIDER={provider!r}. "
        "Use openai, anthropic, google, deepseek, or browser_use."
    )


def _llm_provider_and_model_for_env() -> tuple[str, str]:
    provider = (os.getenv("AGENT_LLM_PROVIDER") or "google").strip().lower() or "google"
    if provider == "openai":
        return provider, os.getenv("OPENAI_MODEL", "gpt-4.1")
    if provider in ("anthropic", "claude"):
        return "anthropic", os.getenv("ANTHROPIC_MODEL", "claude-opus-4-6")
    if provider in ("google", "gemini"):
        return "google", os.getenv("GOOGLE_MODEL", "gemini-flash-latest")
    if provider in ("browser_use", "browser-use", "bu"):
        raw = (os.getenv("BROWSER_USE_MODEL") or "bu-2-0").strip()
        if raw in ("bu-2", "bu2"):
            raw = "bu-2-0"
        return "browser_use", raw
    if provider == "deepseek":
        return "deepseek", _normalize_deepseek_model(os.getenv("DEEPSEEK_MODEL"))
    return provider, ""


def _set_default_timeouts() -> None:
    # Browser-Use uses env vars like TIMEOUT_BrowserStartEvent/TIMEOUT_BrowserLaunchEvent.
    # If Chromium is slow to start (common on first run / constrained machines),
    # the default 30s can be too aggressive.
    os.environ.setdefault("TIMEOUT_BrowserStartEvent", "120")
    os.environ.setdefault("TIMEOUT_BrowserLaunchEvent", "120")


_BROWSER_USE_KEY_DELAY_PATCHED = False


def _apply_browser_use_min_key_delay() -> None:
    """
    browser-use sends synthetic keystrokes with very short asyncio.sleep gaps (often 1–5ms).
    That is unrelated to Browser(wait_between_actions=...), which only pauses between *agent steps*.
    Many sites drop characters unless inter-key delays are longer.

    We floor short sleeps (<= ~30ms) to BROWSER_USE_MIN_KEY_DELAY_MS (default 90ms) by replacing
    asyncio.sleep for this process. Safe here because this module is the main entrypoint.

    Threshold must catch both micro-sleeps (1–10ms) and the ~18ms gap used on some typing paths.
    """
    global _BROWSER_USE_KEY_DELAY_PATCHED
    if _BROWSER_USE_KEY_DELAY_PATCHED:
        return

    raw = os.getenv("BROWSER_USE_MIN_KEY_DELAY_MS", "90")
    try:
        min_s = max(0.0, float(raw) / 1000.0)
    except ValueError:
        min_s = 0.09
    if min_s <= 0:
        return

    thr_raw = os.getenv("BROWSER_USE_MIN_KEY_SLEEP_THRESHOLD_MS", "30")
    try:
        threshold_s = max(0.0, float(thr_raw) / 1000.0)
    except ValueError:
        threshold_s = 0.03

    import asyncio as _asyncio

    _real = _asyncio.sleep

    async def _sleep(delay: float) -> None:
        # Stretch short sleeps used on CDP keystroke paths (includes ~18ms between chars in some branches).
        if delay <= threshold_s:
            delay = max(delay, min_s)
        await _real(delay)

    _asyncio.sleep = _sleep  # type: ignore[assignment]
    _BROWSER_USE_KEY_DELAY_PATCHED = True


_BROWSER_USE_PRE_TYPE_PATCHED = False


def _apply_browser_use_pre_type_delay() -> None:
    """
    Pause briefly after focus/clear and before the first synthetic keystroke.
    Helps with fields that drop leading characters while JS/focus settles.

    Set BROWSER_USE_PRE_TYPE_DELAY_MS (default 280). Use 0 to disable.
    """
    global _BROWSER_USE_PRE_TYPE_PATCHED
    if _BROWSER_USE_PRE_TYPE_PATCHED:
        return

    raw = os.getenv("BROWSER_USE_PRE_TYPE_DELAY_MS", "280")
    try:
        delay_s = max(0.0, float(raw) / 1000.0)
    except ValueError:
        delay_s = 0.28
    if delay_s <= 0:
        _BROWSER_USE_PRE_TYPE_PATCHED = True
        return

    from browser_use.browser.watchdogs.default_action_watchdog import DefaultActionWatchdog

    _orig_input = DefaultActionWatchdog._input_text_element_node_impl
    _orig_page = DefaultActionWatchdog._type_to_page

    async def _input_wrapped(self, *args: Any, **kwargs: Any) -> Any:
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        return await _orig_input(self, *args, **kwargs)

    async def _page_wrapped(self, *args: Any, **kwargs: Any) -> Any:
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        return await _orig_page(self, *args, **kwargs)

    DefaultActionWatchdog._input_text_element_node_impl = _input_wrapped  # type: ignore[assignment]
    DefaultActionWatchdog._type_to_page = _page_wrapped  # type: ignore[assignment]
    _BROWSER_USE_PRE_TYPE_PATCHED = True


def _detect_chromium_executable(explicit: str | None) -> str | None:
    """
    Return a Chromium/Chrome executable path if available.

    We prefer using a system-installed browser binary to avoid Browser-Use trying
    to auto-install via `uvx playwright install ...` (fails if `uvx` isn't installed).
    """
    if explicit:
        p = Path(explicit).expanduser()
        return str(p) if p.exists() else None

    for env_var in ("BROWSER_USE_CHROME_BINARY", "CHROME_BINARY", "CHROME_PATH"):
        v = os.getenv(env_var)
        if v:
            p = Path(v).expanduser()
            if p.exists():
                return str(p)

    for name in ("google-chrome-stable", "google-chrome", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    for p in (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ):
        if Path(p).exists():
            return p

    return None


def _load_json_arg(value: str) -> Any:
    """
    Accept either:
    - a path to a JSON file
    - a raw JSON string
    """
    p = Path(value)
    if p.exists() and p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    return json.loads(value)


def _deep_get(obj: dict[str, Any], path: str) -> Any:
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _deep_set(obj: dict[str, Any], path: str, value: Any) -> None:
    cur: dict[str, Any] = obj
    parts = path.split(".")
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _profile_scalar_from_base_or_custom(profile: dict[str, Any], path: str) -> Any:
    """Read a dotted path from typed `base` first, then from custom relative/absolute maps."""
    v = _deep_get(profile, path)
    if v not in (None, ""):
        return v
    try:
        custom = profile.get("other", {}).get("custom", {})
        if not isinstance(custom, dict):
            return None
        abs_m = custom.get("absolute_fields")
        rel = custom.get("relative_fields")
        if isinstance(abs_m, dict) and path in abs_m and abs_m[path] not in (None, ""):
            return abs_m[path]
        if isinstance(rel, dict) and path in rel and rel[path] not in (None, ""):
            return rel[path]
    except Exception:
        return None
    return None


def _store_base_scalar_in_custom(profile: dict[str, Any], path: str, value: str) -> None:
    """Persist `base.*` scalars under `other.custom.relative_fields` only (no `profile['base']` writes)."""
    other = profile.setdefault("other", {})
    custom = other.setdefault("custom", {})
    if not isinstance(custom, dict):
        other["custom"] = {}
        custom = other["custom"]
    rel = custom.setdefault("relative_fields", {})
    if not isinstance(rel, dict):
        custom["relative_fields"] = {}
        rel = custom["relative_fields"]
    rel[path] = value


def _input_with_periodic_bell(prompt: str) -> str:
    """
    Block for user input.
    """
    # If running inside the orchestrator-managed container, notify the UI that
    # this machine needs human attention while we're blocked on input.
    def _set_orch_attention(needed: bool) -> None:
        base = (os.getenv("ORCH_API_BASE") or "").strip().rstrip("/")
        mid = (os.getenv("ORCH_MACHINE_ID") or "").strip()
        if not base or not mid:
            return
        try:
            payload = {"needed": bool(needed)}
            req = urllib.request.Request(
                f"{base}/api/machines/{mid}/attention",
                method="POST",
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload).encode("utf-8"),
            )
            with urllib.request.urlopen(req, timeout=2.0) as _resp:  # nosec - internal URL
                _ = _resp.read()
        except Exception:
            # Best-effort only: never break the agent on notification failures.
            return

    _set_orch_attention(True)
    try:
        return _sanitize_user_input(input(prompt))
    finally:
        _set_orch_attention(False)


def _orch_post_json(path: str, payload: dict[str, Any], *, timeout_s: float = 4.0) -> bool:
    """
    Best-effort POST to the orchestrator backend (host) from inside the container.
    """
    base = (os.getenv("ORCH_API_BASE") or "").strip().rstrip("/")
    if not base:
        return False
    url = f"{base}{path}"
    try:
        req = urllib.request.Request(
            url,
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # nosec - internal URL only
            _ = resp.read()
            code = int(getattr(resp, "status", 200) or 200)
            return 200 <= code < 300
    except Exception:
        return False


def _flatten_profile_fields(profile: dict[str, Any]) -> dict[str, Any]:
    """
    Best-effort flatten of the profile into stable dotted keys so we can include
    absolute fields in the application record.
    """
    out: dict[str, Any] = {}

    def rec(prefix: str, obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if not isinstance(k, str) or not k:
                    continue
                rec(f"{prefix}.{k}" if prefix else k, v)
            return
        if isinstance(obj, list):
            # Avoid exploding lists; store as JSON string.
            out[prefix] = obj
            return
        out[prefix] = obj

    rec("", profile)
    return out


def _prompt_nonempty(prompt: str, default: str | None = None) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        val = _input_with_periodic_bell(f"\n\033[1;33m{prompt}{suffix}:\033[0m ").strip()
        if not val and default is not None:
            return default
        if val:
            return val
        print("Please enter a value.")


def _prompt_yes_no(prompt: str, default_no: bool = True) -> bool:
    default_hint = "y/N" if default_no else "Y/n"
    val = _input_with_periodic_bell(f"\n\033[1;33m{prompt}\033[0m [{default_hint}]: ").strip().lower()
    if not val:
        return not default_no
    return val in {"y", "yes"}


def _confirm_before_submit_interactive(prompt: str) -> bool:
    """Orchestrator UI when ORCH_* is configured; otherwise terminal Enter-to-confirm."""
    try:
        from agent.orch_human_input import human_input_backend, wait_confirm

        if human_input_backend() == "orch":
            return wait_confirm(action_description=prompt)
    except SystemExit:
        raise
    except Exception:
        pass
    return _prompt_enter_to_confirm(prompt)


def _human_checkpoint_sync(*, orch_message: str, terminal_banner: str, terminal_prompt: str) -> None:
    try:
        from agent.orch_human_input import human_input_backend, wait_captcha_continue

        if human_input_backend() == "orch":
            wait_captcha_continue(message=orch_message)
            return
    except SystemExit:
        raise
    except Exception:
        pass
    print(terminal_banner)
    _input_with_periodic_bell(terminal_prompt)


def _prompt_enter_to_confirm(prompt: str) -> bool:
    """
    Enter-to-confirm prompt used for the submit/continue confirmation step.

    Pressing Enter == confirm. Typing anything starting with 'n' == cancel.
    Any other non-empty input is also treated as cancel so the user can't
    accidentally confirm by smashing keys.
    """
    hint = "press Enter to confirm, or type 'n' to cancel"
    val = _input_with_periodic_bell(
        f"\n\033[1;33m{prompt}\033[0m [{hint}]: "
    ).strip().lower()
    if not val:
        return True
    if val.startswith("n"):
        return False
    # Anything else: treat as "not a clean Enter" → require an explicit
    # confirmation. Safer than accepting a typo.
    print("\033[2m(Input not recognized — treated as cancel. Press Enter to confirm next time.)\033[0m")
    return False


def _prompt_with_default(prompt: str, default: str | None) -> str:
    if default:
        suffix = f" [{default}]"
    else:
        suffix = " [no previous value — type an answer, or press Enter to skip]"
    val = _input_with_periodic_bell(f"\n\033[1;33m{prompt}{suffix}:\033[0m ").strip()
    return default if (not val and default is not None) else val


def _load_profile_from_db(db_path: str, profile_id: str) -> dict[str, Any]:
    # Keep this dependency local to avoid importing project modules when not needed.
    from profiles.store import OrchestratorProfileStore, ProfileStore

    # Prefer orchestrator API (Mongo-backed) when available inside containers.
    if (os.getenv("ORCH_API_BASE") or "").strip():
        try:
            prof = OrchestratorProfileStore().get_profile(profile_id)
            return prof.model_dump(mode="json")
        except Exception:
            # Fall back to file-based store for local runs or if API isn't reachable.
            pass

    store = ProfileStore(db_path)
    try:
        profiles = store.load()
    except ValueError as e:
        raise SystemExit(str(e)) from e
    profile = profiles.get(profile_id)
    if not profile:
        raise SystemExit(f"Profile not found: {profile_id}")
    return profile.model_dump(mode="json")


def _ensure_minimum_profile_fields(profile: dict[str, Any]) -> dict[str, Any]:
    """
    Ensure common required fields are present. The agent can still ask for
    additional missing info during the run via the interactive tool.
    """
    from agent.orch_human_input import (
        extract_scalar_value,
        human_input_backend,
        new_request_id,
        wait_human_response,
    )

    required_paths = [
        ("base.full_name", "Full name"),
        ("base.email", "Email"),
        ("base.phone", "Phone"),
    ]
    for path, label in required_paths:
        cur = _profile_scalar_from_base_or_custom(profile, path)
        if not cur:
            if human_input_backend() == "orch":
                rid = new_request_id()
                pl = path.lower()
                sensitive = "email" in pl or "phone" in pl
                resp = wait_human_response(
                    request_id=rid,
                    kind="field",
                    item={
                        "field_key": path,
                        "display_name": label,
                        "help_text": f"Required profile field missing: {label}",
                        "value_kind": "text",
                        "default_value": None,
                        "options": None,
                        "sensitive": sensitive,
                        "validation": {"required": True},
                        "show_promote_to_absolute": False,
                    },
                    attention_reason=f"profile: {label}",
                )
                val = extract_scalar_value(resp)
                if val is None or str(val).strip() == "":
                    raise SystemExit(f"Missing required profile field: {label}")
                _store_base_scalar_in_custom(profile, path, str(val).strip())
            else:
                _store_base_scalar_in_custom(
                    profile, path, _prompt_nonempty(f"Missing profile field: {label}")
                )
    return profile


def _compact_profile_for_prompt(profile: dict[str, Any]) -> dict[str, Any]:
    """
    Remove null/empty fields from the profile JSON we feed to the LLM.

    Every step replays the initial system message, so shaving dead keys directly
    reduces input tokens per step. We keep the structure (base/other) so the
    agent still understands the layout.
    """

    def clean(v: Any) -> Any:
        if isinstance(v, dict):
            out = {k: clean(x) for k, x in v.items()}
            return {k: x for k, x in out.items() if x not in (None, "", [], {})}
        if isinstance(v, list):
            cleaned = [clean(x) for x in v]
            return [x for x in cleaned if x not in (None, "", [], {})]
        return v

    return clean(profile) or {}


def _profile_for_prompt_without_relative_fields(profile: dict[str, Any]) -> dict[str, Any]:
    """
    Copy of the profile for the initial task text only.

    Job-dependent values live under `other.custom.relative_fields`; omit that
    bucket here so the model does not treat cached answers as authoritative
    before `resolve_fields` confirms them for this application.

    Applicant scalars stored only under custom maps as `base.*` keys are copied
    onto the prompt copy's typed `base` object (ephemeral — not a DB write).
    """
    p = copy.deepcopy(profile)
    other = p.get("other")
    if isinstance(other, dict):
        custom = other.get("custom")
        if isinstance(custom, dict):
            for bucket in ("relative_fields", "absolute_fields"):
                bmap = custom.get(bucket)
                if not isinstance(bmap, dict):
                    continue
                for k, v in list(bmap.items()):
                    if isinstance(k, str) and k.startswith("base.") and v not in (None, ""):
                        _deep_set(p, k, v)
                        del bmap[k]
            if "relative_fields" in custom:
                del custom["relative_fields"]
    return p


def _build_task(job_url: str, profile: dict[str, Any]) -> str:
    """
    The *first* message must contain the profile object (per user request).
    """
    profile_json = json.dumps(
        _compact_profile_for_prompt(_profile_for_prompt_without_relative_fields(profile)),
        ensure_ascii=False,
        indent=2,
    )
    return (
        "You are an expert job application assistant.\n"
        "\n"
        "FIRST ACTION (do this immediately):\n"
        "- Navigate directly to the JOB URL in the browser.\n"
        "- Do not spend time planning before the page is opened.\n"
        "\n"
        "JOB URL:\n"
        f"{job_url}\n"
        "\n"
        "APPLICANT PROFILE (use this as the source of truth):\n"
        f"{profile_json}\n"
        "\n"
        "Goal:\n"
        "- Open the JOB URL and complete the application form(s) using the profile.\n"
        "- If any required information is missing, ask the user for it via the `ask_user_for_missing_info` tool.\n"
        "- CRITICAL: Never guess, invent, or hallucinate values. If you are not confident what a field means or what value is correct,\n"
        "  you MUST ask the user (use `ask_user_for_missing_info` or `resolve_fields`) instead of filling anything.\n"
        "- CRITICAL: Never fill a field without a clear rationale grounded in the applicant profile or explicit instructions on the page.\n"
        "- Before clicking any button that submits an application, advances to the next step, "
        "or performs any irreversible action, you MUST call `confirm_before_submit` and wait for user approval.\n"
        "- CRITICAL — `done` vs `confirm_before_submit`:\n"
        "  - `done` TERMINATES the run. It must be called ONLY after the application has actually been submitted\n"
        "    (or after a hard stop you cannot recover from). NEVER use `done` to pause for user approval,\n"
        "    to ask a question, or to hand control back 'for confirmation' — that will end the run and the\n"
        "    application will NOT be submitted.\n"
        "  - The ONLY correct tool to pause before clicking a submit/continue button is `confirm_before_submit`.\n"
        "    After it returns 'Confirmed.', you MUST click the submit/continue button yourself and then keep\n"
        "    working until the application is actually submitted before calling `done(success=True)`.\n"
        "  - If you are tempted to call `done` with `success=False` because you 'want the user to confirm',\n"
        "    STOP: call `confirm_before_submit` instead. The guard will reject a premature `done` call.\n"
        "- Fields are categorized in the master schema as:\n"
        "  - absolute fields: stable across jobs (name, email, phone, address, etc.)\n"
        "  - relative fields: job-dependent (salary, relocation, notice period, start date, etc.)\n"
        "- If the form asks for ANY relative field (even if you already know a default value), you MUST call `resolve_fields`\n"
        "  BEFORE filling it so the user can review/modify the value for this specific job.\n"
        "- If you encounter any field that is missing from the applicant profile OR required by the form, you MUST call `resolve_fields`\n"
        "  with the field requests BEFORE filling them.\n"
        "- Do not call `resolve_fields` again for the same field keys in later steps unless the form or requirements genuinely changed; the tool\n"
        "  remembers values confirmed earlier in this run and will skip duplicate prompts.\n"
        "- `ask_user_for_missing_info` RESPONSE: read the returned JSON ({key: value}) and fill the form with that value.\n"
        "  NEVER call `ask_user_for_missing_info` twice for the same field — the tool already returned the confirmed value.\n"
        "  If you cannot find a value in the response, use `resolve_fields` (which also returns the cached value for known keys).\n"
        "- Human input UI (orchestrator): for `ask_user_for_missing_info` and each entry in `resolve_fields.fields`, you MUST\n"
        "  include `ui` with `display_name` (normalized label), `value_kind` (text|number|date|multiline|single_select|\n"
        "  multi_select|boolean|file_path), optional `options` [{value,label}] for selects, `sensitive` for secrets,\n"
        "  and optional `validation` {pattern, min, max, required}. Use `file_path` for document paths.\n"
        "- Extra/Other document uploads:\n"
        "  - If an upload field is required and labeled generically (e.g. 'Other', 'Other documents', 'Additional documents')\n"
        "    and the form does NOT explicitly specify which document is needed, FIRST read the whole form.\n"
        "    Use `extract` with a query like: \"What additional/other documents are required or optional in this application?\"\n"
        "    If multiple documents are needed, allow multiple uploads. Distinguish optional vs required.\n"
        "    Then call `resolve_documents` with relative document key(s) (e.g. 'documents.other') BEFORE attempting upload.\n"
        "  - If the form explicitly specifies a document type (e.g. 'Cover letter', 'Portfolio', 'Transcript'),\n"
        "    ALWAYS call `resolve_documents` before uploading so the user can confirm the chosen file(s) for this job.\n"
        "- Any UNKNOWN field discovered in a job application MUST be added as a relative field by default and marked unrecognized.\n"
        "  The user will be asked whether to use its value for all future prompts; if yes, it is promoted to an absolute field.\n"
        "- Submissions must be automatic if the form required only absolute fields.\n"
        "  If any relative fields were involved, require user confirmation before submission.\n"
        "- After any submit/continue attempt, if the page does not advance or anything looks wrong, you MUST read the page for validation errors.\n"
        "  Use the built-in `extract` action with a query like: \"List all visible form validation errors and which field they refer to\".\n"
        "  Then fix the fields and only try to submit again after something has changed.\n"
        "- If a submit/continue button appears disabled, do NOT brute-force clicks.\n"
        "  Instead, read the form for missing/incorrect fields and visible validation errors, fix them, then retry.\n"
        "- If submission repeatedly fails, keep retrying without re-asking for confirmation.\n"
        "  If it fails ~10 times, ask the user to intervene.\n"
        "- If you are struggling to perform any task (e.g., repeated retries, can't find an element, blocked by UI state),\n"
        "  call `request_user_intervention` describing what you need the user to do in the browser.\n"
        "- FORM FILLING / SHADOW DOM (avoid re-filling the same fields in a loop):\n"
        "  - Many application forms render inputs inside **closed or nested Shadow DOM**. The `extract` and `evaluate` "
        "tools read **visible page text and light-DOM**, so they CANNOT reliably read what is currently typed into a "
        "form input. They will routinely report a filled field as `(empty)` even though the value is set.\n"
        "  - **HARD RULE**: if an `input` action returned success for an element index, the field IS filled. Trust it. "
        "Do NOT call `input` again with the **same index** and the **same text** in this run — the orchestrator will "
        "reject the duplicate as a no-op and tell you to move on. The only valid reason to re-type is a URL change or "
        "an explicit form reset visible in the page (e.g. the form was wiped after a navigation).\n"
        "  - **Do NOT** use `extract`, `evaluate`, `find_elements`, or any DOM script to \"verify\" the contents of "
        "text inputs you just filled. Those tools are for reading page content, not input state. If you absolutely "
        "need a visual sanity check, use `screenshot` once — never as a loop guard.\n"
        "  - After filling a batch of text fields once successfully, **move on immediately** to dropdowns, checkboxes, "
        "uploads, `resolve_fields`, and `confirm_before_submit`. Do not chain `wait` / `extract` / `evaluate` steps to "
        "double-check what you just typed.\n"
        "  - If contradictory signals appear (input says OK, extract/evaluate says empty), treat it as a Shadow-DOM "
        "false negative and continue the workflow. If you genuinely cannot proceed because a real validation error "
        "blocks submission, read the page for **validation messages** (not input contents) via `extract` with a query "
        "like \"List all visible form validation errors and which field they refer to\".\n"
        "- DROPDOWNS / SELECT / custom option lists (country, city, etc.):\n"
        "  - Element indices in the browser snapshot are volatile: after any scroll, wait, failed click, or DOM update, "
        "they may change. Do not reuse an index from an earlier step; refresh your picture of the page first.\n"
        "  - Open the control, then target options by exact visible label text (e.g. \"Deutschland\", \"Hamburg\"), "
        "not by remembering a previous index. Prefer `find_elements` or `extract` on the **currently open** list "
        "to map text → the correct clickable row/option.\n"
        "  - Click the innermost clickable node for that row (often `<li>`, `[role='option']`, or the labeled row), "
        "not a generic container that might map to the first item (wrong option).\n"
        "  - Virtualized / long lists: scroll **inside the dropdown’s scrollable panel** (pass that element’s index to "
        "`scroll`), not only the main page, until the target label appears in the list you will click.\n"
        "  - After every click on an option, verify the control now shows the intended value (read the tool result and/or "
        "use `extract`). If it shows a different label, retry with the correct row.\n"
        "  - **CRITICAL — two attempts, then human:** For each dropdown/select (Land, Nationalität, etc.), you get at "
        "most **two complete attempts** to set it (open → pick the correct row → verify the closed control shows the "
        "intended value). **Attempt 1** fails if verification shows the wrong label or still a placeholder like "
        "\"Auswählen\". **Attempt 2** is exactly one more full try with fresh indices/scrolling. If attempt 2 still "
        "does not stick, you **must** call `request_user_intervention` immediately and describe which control to set; "
        "do not try a third automation pass, and do not loop on Escape/click/wait. The runtime may also pause for the "
        "user after repeated placeholder toggles on the same control.\n"
        "- When interacting with CHECKBOXES: after clicking, verify it is actually checked (look for a checked state, aria-checked=true, or UI change).\n"
        "  If the checkbox did not toggle, do NOT keep clicking the outer container.\n"
        "  Instead click the core element: the actual <input type=\"checkbox\"> or the innermost div/span with role=\"checkbox\".\n"
        "  If you first clicked a div and it did not toggle, click the next deeper child element that contains consent/\"I agree\" hint text in ANY language.\n"
        "  Examples of hint text: \"I agree\", \"agree\", \"accept\", \"consent\", \"terms\", \"privacy\", \"Akzeptiere\", \"Ich stimme zu\", \"Einverstanden\", \"J'accepte\", \"Je consens\", \"Acepto\", \"Estoy de acuerdo\", \"Accetto\", \"Sono d'accordo\", \"同意\", \"同意します\", \"同意する\", \"同意する/承諾\", \"同意して\", \"동의\", \"동의합니다\", \"أوافق\", \"موافقة\".\n"
        "  If still not toggled, click the associated <label> text and/or click directly on the small checkbox square (coordinate click if available).\n"
        "- CAPTCHA / human verification (reCAPTCHA, hCaptcha, Turnstile, etc.):\n"
        "  Do NOT try to solve, type into, or click CAPTCHA widgets. That is the user's final manual step.\n"
        "  The script will pause for them; after they finish, automation resumes.\n"
        "  Until then, avoid submits that would only fail on CAPTCHA — fill everything else first.\n"
        "- Prefer filling fields first; only submit when all required fields are satisfied.\n"
    )


def _attachments_dir() -> Path | None:
    """
    Directory where profile-relative document paths resolve (host: repo/attachments,
    agent container: /attachments via bind mount + AGENT_ATTACHMENTS_DIR).
    """
    env = (os.environ.get("AGENT_ATTACHMENTS_DIR") or "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.is_dir() else None
    # Local runs: ./attachments from cwd (e.g. repo root)
    cand = Path.cwd() / "attachments"
    return cand if cand.is_dir() else None


def _resolve_profile_file_ref(raw: str, attachments: Path | None) -> str:
    """Turn a profile path into an absolute filesystem path."""
    s = raw.strip()
    if not s:
        return s
    p = Path(s).expanduser()
    if p.is_absolute():
        return str(p.resolve())
    rel = s.lstrip("./").replace("\\", "/")
    if rel.lower().startswith("attachments/"):
        rel = rel.split("/", 1)[1]
    if attachments is not None:
        return str((attachments / rel).resolve())
    # No attachments dir: resolve relative to cwd
    return str((Path.cwd() / rel).resolve())


def _collect_available_file_paths(profile: dict[str, Any], extra_paths: list[str]) -> list[str]:
    paths: list[str] = []
    attachments = _attachments_dir()

    # Profile-scoped uploads: a name -> repo-relative-path map populated by the
    # orchestrator's `/api/profiles/<id>/attachments` endpoints. Each entry
    # points at a file under `attachments/<profile_id>/`.
    att = profile.get("attachments")
    if isinstance(att, dict):
        for v in att.values():
            if isinstance(v, str) and v.strip():
                paths.append(_resolve_profile_file_ref(v.strip(), attachments))

    paths.extend(_resolve_profile_file_ref(p, attachments) for p in extra_paths if p.strip())

    # Allow every regular file under the attachments mount (orchestrator bind-mounts repo attachments/)
    if attachments is not None:
        try:
            for f in sorted(attachments.rglob("*")):
                if f.is_file():
                    paths.append(str(f.resolve()))
        except OSError:
            pass

    # Normalize and keep only existing files (Browser-Use will error if you pass
    # non-existent paths)
    out: list[str] = []
    seen: set[str] = set()
    for p in paths:
        pp = str(Path(p).expanduser())
        if pp in seen:
            continue
        try:
            rp = Path(pp).resolve()
        except OSError:
            continue
        if rp.is_file():
            out.append(str(rp))
            seen.add(str(rp))
    return out


# JavaScript injected into the page to scroll to the form control that the user
# is being asked about and flash-highlight its container. It is intentionally
# self-contained and tolerant of missing hints — the agent passes label text
# (e.g. "Available from", "Verfügbar ab") plus an internal key (e.g.
# "application.available_from") and we try each hint against labels, aria
# attributes, placeholders, name/id, etc.
_FIELD_HIGHLIGHT_JS = r"""
(() => {
  const data = __HIGHLIGHT_PAYLOAD__;
  const hints = [];
  const push = (v) => {
    if (!v) return;
    const s = String(v).trim();
    if (!s) return;
    hints.push(s);
  };
  push(data.label);
  push(data.prompt);
  // Last path segment of dotted keys, e.g. "application.available_from" -> "available_from".
  if (data.key) {
    const segs = String(data.key).split('.');
    push(segs[segs.length - 1]);
    push(String(data.key).replace(/_/g, ' '));
  }

  const norm = (s) => (s || '')
    .toString()
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  const normHints = Array.from(new Set(hints.map(norm))).filter(Boolean);
  if (!normHints.length) return { matched: false, reason: 'no-hints' };

  const scoreText = (text) => {
    const t = norm(text);
    if (!t) return 0;
    let s = 0;
    for (const h of normHints) {
      if (!h) continue;
      if (t === h) s = Math.max(s, 1000);
      else if (t.includes(h)) s = Math.max(s, 500 + h.length);
      else if (h.includes(t) && t.length >= 3) s = Math.max(s, 200 + t.length);
    }
    return s;
  };

  const isField = (el) => el && el.matches &&
    el.matches('input:not([type=hidden]), textarea, select, [role=combobox], [role=listbox], [contenteditable="true"]');

  const findInputFor = (labelEl) => {
    if (!labelEl) return null;
    const htmlFor = labelEl.getAttribute && labelEl.getAttribute('for');
    if (htmlFor) {
      const el = document.getElementById(htmlFor);
      if (el && isField(el)) return el;
    }
    const inner = labelEl.querySelector && labelEl.querySelector('input, textarea, select, [contenteditable="true"]');
    if (inner) return inner;
    // Look at siblings inside the same form-row container.
    let p = labelEl.parentElement;
    for (let depth = 0; depth < 4 && p; depth++) {
      const sib = p.querySelector('input:not([type=hidden]), textarea, select, [contenteditable="true"]');
      if (sib) return sib;
      p = p.parentElement;
    }
    return null;
  };

  const candidates = [];
  // 1) <label> elements.
  document.querySelectorAll('label').forEach((lab) => {
    const sc = scoreText(lab.textContent || '');
    if (sc > 0) {
      const input = findInputFor(lab);
      if (input) candidates.push({ score: sc + 30, input, labelEl: lab });
    }
  });
  // 2) <legend> inside fieldsets.
  document.querySelectorAll('fieldset > legend, [role=group] > :first-child').forEach((lg) => {
    const sc = scoreText(lg.textContent || '');
    if (sc > 0) {
      const fs = lg.closest('fieldset, [role=group]');
      const input = fs && fs.querySelector('input:not([type=hidden]), textarea, select, [contenteditable="true"]');
      if (input) candidates.push({ score: sc + 15, input, labelEl: lg });
    }
  });
  // 3) Attribute-based matches on the fields themselves.
  const attrFields = document.querySelectorAll(
    'input:not([type=hidden]), textarea, select, [role=combobox]'
  );
  attrFields.forEach((el) => {
    let sc = 0;
    for (const attr of ['aria-label', 'placeholder', 'name', 'id', 'data-testid', 'data-test', 'title']) {
      sc = Math.max(sc, scoreText(el.getAttribute && el.getAttribute(attr)));
    }
    const labelledBy = el.getAttribute && el.getAttribute('aria-labelledby');
    if (labelledBy) {
      labelledBy.split(/\s+/).forEach((id) => {
        const node = id && document.getElementById(id);
        if (node) sc = Math.max(sc, scoreText(node.textContent || ''));
      });
    }
    if (sc > 0) candidates.push({ score: sc, input: el, labelEl: null });
  });

  if (!candidates.length) return { matched: false, reason: 'no-candidates' };

  // Prefer visible elements (non-zero box) and the highest-scoring match.
  candidates.sort((a, b) => b.score - a.score);
  const chosen = candidates.find(({ input }) => {
    if (!input.getBoundingClientRect) return false;
    const r = input.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  }) || candidates[0];
  if (!chosen) return { matched: false, reason: 'no-visible' };

  const pickContainer = (el) => {
    let cur = el;
    for (let i = 0; i < 6 && cur; i++) {
      const parent = cur.parentElement;
      if (!parent) break;
      const cls = (parent.className && String(parent.className)) || '';
      if (/field|form-row|form-group|form-item|row|question|input|control/i.test(cls)) return parent;
      if (parent.tagName === 'FIELDSET' || parent.tagName === 'LABEL') return parent;
      cur = parent;
    }
    return el;
  };
  const container = pickContainer(chosen.input);

  // Ensure style tag and cleanup of previous highlights.
  const STYLE_ID = '__octopilot_field_highlight_style__';
  if (!document.getElementById(STYLE_ID)) {
    const s = document.createElement('style');
    s.id = STYLE_ID;
    s.textContent = `
      [data-octopilot-highlight] {
        outline: 3px solid #f59e0b !important;
        outline-offset: 2px !important;
        box-shadow: 0 0 0 6px rgba(245, 158, 11, 0.25) !important;
        border-radius: 6px !important;
        transition: outline-color 200ms ease, box-shadow 200ms ease !important;
        animation: __octopilot_field_pulse__ 1200ms ease-in-out 3 !important;
      }
      @keyframes __octopilot_field_pulse__ {
        0%   { box-shadow: 0 0 0 4px rgba(245, 158, 11, 0.15); }
        50%  { box-shadow: 0 0 0 10px rgba(245, 158, 11, 0.45); }
        100% { box-shadow: 0 0 0 4px rgba(245, 158, 11, 0.15); }
      }
    `;
    (document.head || document.documentElement).appendChild(s);
  }
  document.querySelectorAll('[data-octopilot-highlight]').forEach((el) => {
    el.removeAttribute('data-octopilot-highlight');
  });

  container.setAttribute('data-octopilot-highlight', '1');
  try {
    container.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
  } catch (_e) {
    try { container.scrollIntoView(); } catch (_e2) {}
  }

  // Auto-clear after a timeout so we don't leave stale outlines around if
  // the agent navigates away or the user takes a long time.
  const TIMEOUT_MS = data.timeout_ms || 12000;
  setTimeout(() => {
    try { container.removeAttribute('data-octopilot-highlight'); } catch (_e) {}
  }, TIMEOUT_MS);

  return {
    matched: true,
    score: chosen.score,
    tag: (chosen.input.tagName || '').toLowerCase(),
    name: chosen.input.getAttribute && (chosen.input.getAttribute('name') || chosen.input.getAttribute('id') || ''),
  };
})();
"""


async def _cdp_highlight_field(
    browser_session: Any,
    *,
    label: str | None,
    key: str | None,
    prompt: str | None,
    timeout_ms: int = 12000,
) -> None:
    """Run the field-highlight script inside the current page via CDP."""
    if browser_session is None:
        return
    try:
        cdp_session = await browser_session.get_or_create_cdp_session()
    except Exception:
        return
    payload = {
        "label": label or "",
        "key": key or "",
        "prompt": prompt or "",
        "timeout_ms": int(max(1000, timeout_ms)),
    }
    script = _FIELD_HIGHLIGHT_JS.replace(
        "__HIGHLIGHT_PAYLOAD__",
        json.dumps(payload, ensure_ascii=False),
    )
    try:
        await asyncio.wait_for(
            cdp_session.cdp_client.send.Runtime.evaluate(
                params={"expression": script, "returnByValue": True, "awaitPromise": False},
                session_id=cdp_session.session_id,
            ),
            timeout=2.5,
        )
    except Exception:
        return


def _install_field_highlighter(
    *,
    agent_box: dict[str, Any],
    loop: asyncio.AbstractEventLoop,
    profiler: Any,
) -> None:
    """
    Wire profiler.ui.field_highlighter to a callback that — given a label/key —
    executes `_cdp_highlight_field` on the running agent's browser session.

    The profiler runs tool actions in a worker thread, so we hop back to the
    main asyncio loop via `run_coroutine_threadsafe` to talk to CDP.
    """

    def _sync_highlight(label: str | None, key: str | None, prompt: str | None) -> None:
        a = agent_box.get("agent")
        sess = getattr(a, "browser_session", None) if a else None
        if sess is None or loop.is_closed():
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                _cdp_highlight_field(sess, label=label, key=key, prompt=prompt),
                loop,
            )
            # Wait briefly so the outline is visible by the time the prompt
            # appears; give up silently if the page is busy.
            fut.result(timeout=2.5)
        except Exception:
            return

    try:
        ui = getattr(profiler, "ui", None)
        if ui is not None:
            setattr(ui, "field_highlighter", _sync_highlight)
    except Exception:
        return


async def _run(
    job_url: str,
    profile: dict[str, Any],
    *,
    max_steps: int,
    headless: bool,
    available_files: list[str],
    db_path: str,
    profile_id: str,
    keep_open: bool,
) -> int:
    # browser-use is optional until runtime
    from browser_use import Agent, Browser, Tools

    _apply_browser_use_min_key_delay()
    _apply_browser_use_pre_type_delay()

    tools = Tools()
    submit_guard: dict[str, int] = {}
    submit_confirmed_once: dict[str, bool] = {}
    agent_box: dict[str, Any] = {}
    # Tracks whether `confirm_before_submit` ran at least once this run. Used by
    # the `done` guard below to reject premature terminations where the agent
    # tried to hand control back "for confirmation" instead of actually pausing
    # via `confirm_before_submit` (see log4.txt).
    done_guard_state: dict[str, Any] = {
        "confirm_called": False,
        "rejections": 0,
    }
    _DONE_GUARD_MAX_REJECTIONS = int(os.getenv("AGENT_DONE_GUARD_MAX_REJECTIONS", "2") or 2)
    profiler = Profiler(db_path=db_path, profile_id=profile_id)
    run_id = str(uuid.uuid4())
    # Fields the agent explicitly resolved (relative/unknown/doc uploads) + stable profile snapshot.
    resolved_fields: dict[str, Any] = {}
    profile_fields = _flatten_profile_fields(profile)
    # Every string typed into a browser input during this run. Used to infer which
    # profile fields were actually present on the job form (cf. `_form_fields_only`).
    typed_values: list[str] = []

    def _form_fields_only() -> dict[str, Any]:
        """
        Return only the fields that were actually present on the job application form:
        - every key explicitly resolved via `resolve_fields` / `ask_user_for_missing_info`
        - every profile field whose value was typed verbatim into a browser input.
        This avoids dumping the entire applicant profile into the finished-application record.
        """
        typed_set: set[str] = set()
        for raw in typed_values:
            if not isinstance(raw, str):
                continue
            s = raw.strip()
            if len(s) >= 2:
                typed_set.add(s)
                typed_set.add(s.lower())
        picked: dict[str, Any] = {}
        for k, v in profile_fields.items():
            if not isinstance(v, (str, int, float)) or isinstance(v, bool):
                continue
            sv = str(v).strip()
            if len(sv) < 2:
                continue
            if sv in typed_set or sv.lower() in typed_set:
                picked[k] = v
        picked.update(resolved_fields)
        return picked

    def _persist_result(status: str, description: str) -> None:
        # Persist run record (best-effort) back to orchestrator (host)
        mid = (os.getenv("ORCH_MACHINE_ID") or "").strip()
        if not mid:
            return
        _orch_post_json(
            f"/api/machines/{mid}/application-result",
            {
                "run_id": run_id,
                "application_url": job_url,
                "status": status,
                "description": description,
                "fields": _form_fields_only(),
            },
            timeout_s=6.0,
        )

    def _preflight_url_missing(url: str) -> tuple[bool, str]:
        """
        Best-effort check to detect removed application pages early.

        We only hard-stop on explicit 404/410 responses; anything else is treated as "unknown"
        and we proceed (some sites block non-browser clients with 403/429, etc).
        """
        u = (url or "").strip()
        if not u:
            return True, "Empty application URL."
        try:
            req = urllib.request.Request(
                u,
                method="GET",
                headers={"User-Agent": "octopilot-agent/1.0"},
            )
            with urllib.request.urlopen(req, timeout=8.0) as resp:  # nosec - user-provided URL (GET only)
                code = int(getattr(resp, "status", 200) or 200)
                if code in (404, 410):
                    return True, f"HTTP {code}"
                return False, ""
        except urllib.error.HTTPError as e:
            code = int(getattr(e, "code", 0) or 0)
            if code in (404, 410):
                return True, f"HTTP {code}"
            return False, ""
        except Exception:
            return False, ""

    missing, missing_hint = _preflight_url_missing(job_url)
    if missing:
        status = "Not found"
        description = (
            f"The provided job application URL ({job_url}) appears to be unavailable ({missing_hint}). "
            "Stopping the operation."
        )
        _persist_result(status, description)
        print("\n=== Final result ===\n")
        print(description)
        return 2

    @tools.action(
        description=(
            "Ask the user for a single missing value. Returns the value (as JSON {key: value}) so you "
            "can fill it into the form immediately. If this field was already answered earlier in this run, "
            "it returns the cached value instead of re-prompting. "
            "You MUST pass `ui` with display_name, value_kind (text|number|date|multiline|single_select|"
            "multi_select|boolean|file_path), optional options for selects, sensitive=true for secrets, "
            "and validation hints (pattern/min/max)."
        ),
        param_model=AskUserMissingInfoParams,
    )
    def ask_user_for_missing_info(params: AskUserMissingInfoParams) -> str:
        field_path = params.field_path
        question = params.question
        key = (field_path or "").strip() or "<unknown>"

        spec_ui = params.ui or FieldUiSpec(
            display_name=key,
            value_kind="multiline",
            help_text=question,
        )
        req = ResolveFieldsParams(
            fields=[
                FieldRequest(
                    key=field_path,
                    label=field_path,
                    prompt=question,
                    default=None,
                    ui=spec_ui,
                )
            ]
        )
        raw = profiler.resolve_fields(req)

        # Merge into resolved_fields for persistence parity with `resolve_fields`.
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                resolved_fields.update(parsed)
        except Exception:
            parsed = None

        value = None
        if isinstance(parsed, dict):
            value = parsed.get(key)

        if value not in (None, ""):
            return (
                f"{raw}\n"
                f"(Use this value for {field_path} — it was confirmed by the user and stored for this run. "
                "Fill the form now; do NOT call ask_user_for_missing_info for this field again.)"
            )
        return raw or "{}"

    @tools.action(
        description=(
            "Resolve fields needed by the job application form. "
            "Shows defaults (last used), lets user modify, and optionally saves updates in the master schema store."
        ),
        param_model=ResolveFieldsParams,
    )
    def resolve_fields(params: ResolveFieldsParams) -> str:
        raw = profiler.resolve_fields(params)
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                resolved_fields.update(parsed)
        except Exception:
            pass
        return raw

    @tools.action(
        description=(
            "Resolve document uploads required by the job application. "
            "Prompts user for file paths when needed, whitelists them for upload, and stores last-used values."
        ),
        param_model=ResolveDocumentsParams,
    )
    def resolve_documents(params: ResolveDocumentsParams) -> str:
        raw = profiler.resolve_documents(params, available_files=available_files)
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                resolved_fields.update(parsed)
        except Exception:
            pass
        return raw

    @tools.action(description="Ask user for confirmation before submitting/continuing an application step.")
    async def confirm_before_submit(action_description: str) -> str:
        done_guard_state["confirm_called"] = True
        # CAPTCHA should be solved as the very last manual step, right before submission/continue.
        # So we only pause here (not earlier), after the agent has filled all fields it can.
        a = agent_box.get("agent")
        sess = getattr(a, "browser_session", None) if a else None
        if sess is not None:
            try:
                state = await sess.get_browser_state_summary()
            except Exception:
                state = None

            if state is not None and _browser_state_suggests_captcha(state):
                orch_msg = (
                    "CAPTCHA or human verification was detected in the browser. "
                    "Solve it in the VNC window, then press Continue in the Input tab to proceed with submission."
                )
                term_banner = (
                    "\nCAPTCHA / human verification detected.\n"
                    "Please solve it in the browser window now (this is the last step before submit).\n"
                    "When done, press Enter here to continue with submission.\n"
                )
                await asyncio.to_thread(
                    _human_checkpoint_sync,
                    orch_message=orch_msg,
                    terminal_banner=term_banner,
                    terminal_prompt="\n\033[1;33mPress Enter to continue...\033[0m ",
                )

        # Track repeated attempts for the same submit/continue action.
        page_url = None
        if state is not None:
            page_url = getattr(state, "url", None)
        key = _stable_submit_key(action_description, page_url)
        submit_guard[key] = submit_guard.get(key, 0) + 1

        # If no relative fields were used, submission should be automatic.
        if not profiler.relative_used_in_current_form:
            # Keep a small breadcrumb so it's obvious why no prompt happened.
            print(f"\n\033[2mAuto-submitting: no relative fields were resolved for this form.\033[0m\n")
            return "Confirmed (auto: no uncertain fields)."

        # Ask for confirmation only once per action; subsequent retries should not re-prompt.
        if not submit_confirmed_once.get(key, False):
            ok = await asyncio.to_thread(
                _confirm_before_submit_interactive,
                f"Confirm: {action_description}",
            )
            if not ok:
                raise InterruptedError("User declined confirmation.")
            submit_confirmed_once[key] = True

        # If it keeps failing, require user intervention (but do not ask confirmation again).
        if submit_guard[key] >= 10:
            orch_msg = (
                "Submission failed many times. Fix the form or CAPTCHA in the VNC window, "
                "then press Continue in the Input tab so the agent can retry."
            )
            term_banner = (
                "\n\033[1;31mSubmission appears to be failing repeatedly (10+ attempts).\033[0m\n"
                "Please intervene manually in the browser window:\n"
                "- If submit is disabled, look for missing/incorrect fields\n"
                "- Read visible validation errors\n"
                "- Fix required fields\n"
                "- Solve CAPTCHA if present\n"
                "Then press Enter to let the agent retry.\n"
            )
            await asyncio.to_thread(
                _human_checkpoint_sync,
                orch_message=orch_msg,
                terminal_banner=term_banner,
                terminal_prompt="\n\033[1;33mPress Enter to continue...\033[0m ",
            )
            submit_guard[key] = 0

        return "Confirmed."

    def _install_done_guard() -> None:
        """
        Wrap the built-in `done` action so the agent cannot use it to pause for
        user approval. Symptom we're fixing (log4.txt): the agent called
        `done(success=False, text="... I need your confirmation before ...")`
        which *terminates* the run; nothing ever gets submitted.

        Policy:
          - If `confirm_before_submit` has already been called this run, allow
            `done` through (agent is reporting the final outcome).
          - Otherwise, if `done` is called with `success=False` OR the text
            looks like "needs confirmation / please confirm / ready to submit",
            reject it: return a non-terminating ActionResult with an error that
            tells the agent to call `confirm_before_submit` instead.
          - After `_DONE_GUARD_MAX_REJECTIONS` rejections in a row, stop
            fighting the model and let `done` through (fail-safe so we never
            get stuck in an infinite rejection loop).
        """
        try:
            from browser_use.agent.views import ActionResult  # type: ignore
        except Exception:
            return

        actions = getattr(getattr(tools, "registry", None), "registry", None)
        actions = getattr(actions, "actions", None) if actions is not None else None
        if not isinstance(actions, dict) or "done" not in actions:
            return

        registered = actions["done"]
        original_fn = getattr(registered, "function", None)
        if not callable(original_fn):
            return

        _CONFIRMATION_HINTS = (
            "need your confirmation",
            "before clicking",
            "before i submit",
            "before submitting",
            "ready to submit",
            "please confirm",
            "awaiting confirmation",
            "confirmation before",
            "bewerbung senden",  # the German submit-button text from the failing run
        )

        async def guarded_done(**kwargs: Any) -> Any:
            params = kwargs.get("params")
            success = bool(getattr(params, "success", False))
            text = str(getattr(params, "text", "") or "")
            text_l = text.lower()
            looks_like_pause = (
                not success
                or any(h in text_l for h in _CONFIRMATION_HINTS)
            )

            # Already confirmed submission at least once → allow the agent to
            # report its final result through `done` (success or failure).
            if done_guard_state["confirm_called"]:
                return await original_fn(**kwargs)

            # If nothing in the task required confirmation (no relative fields),
            # don't obstruct `done` either — submissions are auto in that case.
            if not getattr(profiler, "relative_used_in_current_form", True):
                return await original_fn(**kwargs)

            # Fail-safe: if we've rejected too many times, stop obstructing so
            # the run can still terminate.
            if done_guard_state["rejections"] >= _DONE_GUARD_MAX_REJECTIONS:
                return await original_fn(**kwargs)

            if not looks_like_pause:
                # `done(success=True, text=...)` that does NOT look like a pause
                # is unusual when confirm_before_submit was never called — but
                # we don't want to misclassify a genuine completion. Allow it.
                return await original_fn(**kwargs)

            done_guard_state["rejections"] += 1
            msg = (
                "REJECTED: `done` was called before `confirm_before_submit`, and the message "
                "reads like a request for the user to confirm submission. "
                "`done` TERMINATES the run — it cannot be used to pause for approval. "
                "Call `confirm_before_submit(action_description=...)` to ask the user, "
                "wait for 'Confirmed.', then click the submit/continue button. "
                "Only call `done(success=True)` AFTER the application is actually submitted."
            )
            print(
                f"\n\033[1;31m[done-guard]\033[0m premature `done` blocked "
                f"(rejection {done_guard_state['rejections']}/{_DONE_GUARD_MAX_REJECTIONS}). "
                "Nudging the agent to call `confirm_before_submit` instead.\n"
            )
            return ActionResult(
                is_done=False,
                success=False,
                error=msg,
                long_term_memory=(
                    "The `done` tool was rejected because `confirm_before_submit` "
                    "had not been called yet. Use `confirm_before_submit` to ask "
                    "the user before clicking any submit/continue button, then "
                    "actually submit before calling `done(success=True)`."
                ),
            )

        registered.function = guarded_done  # type: ignore[assignment]

    _install_done_guard()

    def _install_input_dedup_guard() -> None:
        """
        Wrap the built-in `input` action so the agent cannot re-fill the same
        text into the same field on the same URL.

        Symptom we're fixing (this run, log...): the model successfully fills
        every text input in the form, then either (a) loses the action list to
        an LLM-output validation error and restarts the step from scratch, or
        (b) calls `extract` / `evaluate` to "verify" the values, which on
        Shadow-DOM application forms reports every input as **(empty)** even
        though the value is actually applied. The model interprets that false
        negative as proof the fields are blank and re-types every value — the
        whole batch is replayed multiple times in a row, wasting tokens and
        often jumping focus around or breaking the form.

        Policy:
          - Track every (url, element_index, normalized_text) triple that has
            already been typed successfully in this run.
          - When the agent calls `input` with a triple we've already seen,
            short-circuit: return a *successful* ActionResult that tells the
            agent "this field is already filled, move on", without poking the
            browser again.
          - Identical short strings (<2 chars) are not deduped — they're more
            likely to legitimately repeat across unrelated controls.
        """
        try:
            from browser_use.agent.views import ActionResult  # type: ignore
        except Exception:
            return

        actions = getattr(getattr(tools, "registry", None), "registry", None)
        actions = getattr(actions, "actions", None) if actions is not None else None
        if not isinstance(actions, dict) or "input" not in actions:
            return

        registered_input = actions["input"]
        original_input_fn = getattr(registered_input, "function", None)
        if not callable(original_input_fn):
            return

        history: list[tuple[str, int, str]] = []
        _MAX_HISTORY = 128

        def _norm(s: str) -> str:
            return (s or "").strip().casefold()

        async def _safe_current_url(browser_session: Any) -> str:
            if browser_session is None:
                return ""
            getter = getattr(browser_session, "get_current_page_url", None)
            if not callable(getter):
                return ""
            try:
                v = await getter()
                return str(v or "")
            except Exception:
                return ""

        async def guarded_input(**kwargs: Any) -> Any:
            params = kwargs.get("params")
            if params is None:
                return await original_input_fn(**kwargs)

            text_raw = getattr(params, "text", "") or ""
            try:
                index = int(getattr(params, "index", -1) or -1)
            except Exception:
                index = -1

            if index < 0 or len(text_raw.strip()) < 2:
                return await original_input_fn(**kwargs)

            norm = _norm(text_raw)
            url = await _safe_current_url(kwargs.get("browser_session"))

            for past_url, past_idx, past_text in history:
                if past_idx == index and past_text == norm and past_url == url:
                    msg = (
                        f"Skipped duplicate input: the value {text_raw!r} was "
                        f"already typed into element index {index} on this page "
                        "earlier in this run. The first `input` succeeded; the "
                        "field is filled. DO NOT call `input` again with the "
                        "same index and the same text. DO NOT use `extract` or "
                        "`evaluate` to verify input-field contents — application "
                        "forms commonly render inputs in Shadow DOM and those "
                        "tools will report (empty) even when the value is set. "
                        "If you really need a sanity check, take a `screenshot`. "
                        "Otherwise move on to the next pending control: "
                        "dropdown, checkbox, file upload, `resolve_fields`, or "
                        "`confirm_before_submit`."
                    )
                    print(
                        "\n\033[1;33m[input-dedup]\033[0m duplicate input "
                        f"blocked (index={index}, text={text_raw!r}). Telling "
                        "the agent to move on.\n"
                    )
                    return ActionResult(
                        success=True,
                        extracted_content=(
                            f"Skipped duplicate input for index {index} "
                            f"(value already typed)."
                        ),
                        long_term_memory=msg,
                        include_in_memory=True,
                    )

            result = await original_input_fn(**kwargs)
            try:
                errored = bool(getattr(result, "error", None))
            except Exception:
                errored = False
            if not errored:
                history.append((url, index, norm))
                if len(history) > _MAX_HISTORY:
                    del history[: len(history) - _MAX_HISTORY]
            return result

        registered_input.function = guarded_input  # type: ignore[assignment]

    _install_input_dedup_guard()

    def _install_dropdown_intervention_guard() -> None:
        """
        If the agent clicks the same placeholder-style dropdown trigger (e.g. "Auswählen")
        twice on the same URL/index without selecting a list option in between, pause for
        human intervention. Matches the policy: after the second failed handling attempt,
        the user must set the control so automation does not spin in dropdown loops.
        """
        try:
            from browser_use.agent.views import ActionResult  # type: ignore
        except Exception:
            return

        actions = getattr(getattr(tools, "registry", None), "registry", None)
        actions = getattr(actions, "actions", None) if actions is not None else None
        if not isinstance(actions, dict) or "click" not in actions:
            return

        registered = actions["click"]
        original_fn = getattr(registered, "function", None)
        if not callable(original_fn):
            return

        try:
            threshold = int(os.getenv("AGENT_DROPDOWN_PLACEHOLDER_CLICKS_FOR_HUMAN", "2") or 2)
        except ValueError:
            threshold = 2
        threshold = max(2, threshold)

        # (page_url, element_index) -> consecutive placeholder-toggle clicks
        toggle_counts: dict[tuple[str, int], int] = {}

        # Button labels that mean "no selection yet" (expand as needed).
        _PLACEHOLDER_SUBSTR = (
            "auswählen",
            "bitte wählen",
            "please select",
            "select...",
            "choose...",
            "wählen sie",
            "select an option",
            "-- select",
        )

        def _click_describes_option_pick(content: str) -> bool:
            c = content.casefold()
            if "clicked li" in c:
                return True
            if "clicked option" in c:
                return True
            if "role='option'" in c or '[role="option"]' in c:
                return True
            return False

        def _click_describes_placeholder_toggle(content: str) -> bool:
            """True if this looks like clicking a closed combobox that still shows a placeholder label."""
            if not content or _click_describes_option_pick(content):
                return False
            c = content.casefold()
            if "clicked button" not in c and "clicked combobox" not in c:
                return False
            # Quoted control label in tool output, e.g. Clicked button "Auswählen"
            part = content
            for token in ('"', "\u201c", "\u201d"):
                part = part.replace(token, '"')
            lower = part.casefold()
            for sub in _PLACEHOLDER_SUBSTR:
                if sub in lower:
                    return True
            return False

        async def _safe_current_url(browser_session: Any) -> str:
            if browser_session is None:
                return ""
            getter = getattr(browser_session, "get_current_page_url", None)
            if not callable(getter):
                return ""
            try:
                v = await getter()
                return str(v or "")
            except Exception:
                return ""

        async def guarded_click(**kwargs: Any) -> Any:
            result = await original_fn(**kwargs)
            try:
                content = str(getattr(result, "extracted_content", "") or "")
            except Exception:
                content = ""

            params = kwargs.get("params")
            try:
                index = int(getattr(params, "index", -1) or -1) if params is not None else -1
            except Exception:
                index = -1

            url = await _safe_current_url(kwargs.get("browser_session"))

            if _click_describes_option_pick(content):
                for k in list(toggle_counts.keys()):
                    if k[0] == url:
                        del toggle_counts[k]
                return result

            if index < 0 or not _click_describes_placeholder_toggle(content):
                return result

            key = (url, index)
            toggle_counts[key] = toggle_counts.get(key, 0) + 1
            if toggle_counts[key] < threshold:
                return result

            toggle_counts[key] = 0
            orch_msg = (
                "The agent clicked the same dropdown trigger twice without choosing an option (or the selection "
                "did not apply). Set this control manually in the browser, then continue."
            )
            term_banner = (
                "\n\033[1;31mDropdown handling failed after repeated tries on the same control.\033[0m\n"
                "Please open the dropdown in the browser window, choose the correct value, and close it.\n"
                "Then press Enter here to resume automation.\n"
            )
            print(
                "\n\033[1;33m[dropdown-guard]\033[0m placeholder toggles reached threshold "
                f"(url={url[:80]!r}…, index={index}, threshold={threshold}). Pausing for human.\n"
            )
            try:
                await asyncio.to_thread(
                    _human_checkpoint_sync,
                    orch_message=orch_msg,
                    terminal_banner=term_banner,
                    terminal_prompt="\n\033[1;33mPress Enter to continue...\033[0m ",
                )
            except Exception:
                pass

            return ActionResult(
                success=True,
                extracted_content=(
                    f"{content}\n\n[dropdown-guard] Human checkpoint completed after {threshold} placeholder "
                    "toggles on this control. Re-read the page and continue; call `request_user_intervention` "
                    "yourself if it still cannot be set automatically."
                ),
                include_in_memory=True,
                long_term_memory=(
                    "You hit the maximum automated tries for this dropdown trigger. The user was asked to set the "
                    "control manually. Read the current UI state and continue with the next fields; do not repeat "
                    "the same open/close click loop."
                ),
            )

        registered.function = guarded_click  # type: ignore[assignment]

    _install_dropdown_intervention_guard()

    @tools.action(
        description=(
            "Ask the user to manually complete a difficult step in the browser and then resume. "
            "Use this when stuck after multiple retries (e.g. UI blockers, complex widgets, non-automatable steps)."
        )
    )
    def request_user_intervention(task: str, what_to_do: str | None = None) -> str:
        msg = (
            "\n\033[1;31mUser intervention requested.\033[0m\n"
            f"Task: {task}\n"
        )
        if what_to_do:
            msg += f"\nWhat to do in the browser:\n{what_to_do}\n"
        msg += "\nAfter you complete it in the browser, press Enter here to resume.\n"
        print(msg)
        _input_with_periodic_bell("\n\033[1;33mPress Enter to continue...\033[0m ")
        return "User intervention completed. Continue."

    chrome_binary = _detect_chromium_executable(os.getenv("BROWSER_USE_BROWSER_BINARY"))
    if not chrome_binary:
        raise SystemExit(
            "Could not find a Chromium/Chrome executable.\n"
            "Install one of: google-chrome, chromium, or set an explicit path via:\n"
            '  export BROWSER_USE_BROWSER_BINARY="/path/to/chrome"\n'
            "This avoids Browser-Use attempting an auto-install via `uvx`, which is missing on your system."
        )

    # Browser-Use will add `--start-maximized` by default when NOT headless
    # unless we set an explicit window_size. We set window_size + window_position
    # explicitly: right-half on a wide desktop, or full virtual screen in Docker
    # (AGENT_BROWSER_FULL_DISPLAY=1, portrait Xvfb).
    window_size = None
    window_position = None
    if not headless:
        mx, my, sw, sh = _get_primary_monitor_geometry()
        full_display = (os.getenv("AGENT_BROWSER_FULL_DISPLAY") or "").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        if full_display:
            window_size = {"width": sw, "height": sh}
            window_position = {"width": mx, "height": my}
            print("********** Window Positioning (full display) **********")
            print(f"Monitor Geometry: {mx}, {my}, {sw}, {sh}")
            print(f"Window Size: {sw}, {sh}")
            print(f"Window Position: {mx}, {my}")
            print("********************************************************")
        else:
            half_w = max(400, sw // 2)
            window_size = {"width": half_w, "height": sh}
            # Right half of the PRIMARY monitor, respecting monitor offset
            window_position = {"width": mx + (sw - half_w), "height": my}
            print("********** Window Positioning **********")
            print(f"Monitor Geometry: {mx}, {my}, {sw}, {sh}")
            print(f"Window Size: {half_w}, {sh}")
            print(f"Window Position: {mx + (sw - half_w)}, {my}")
            print(f"***************************************")
    # On Wayland, many compositors ignore app-requested absolute window positioning.
    # Forcing Chrome to use the X11 backend (Xwayland) usually restores predictable
    # window placement for --window-position/window_size.
    browser_args = None
    if (os.getenv("XDG_SESSION_TYPE") or "").lower() == "wayland" and not headless:
        browser_args = ["--ozone-platform=x11"]

    browser_kwargs: dict[str, Any] = {
        "executable_path": chrome_binary,
        "headless": headless,
        "window_size": window_size,
        "window_position": window_position,
        "args": browser_args,
        # Pauses between *high-level* browser actions (not per keystroke). For missed characters,
        # use BROWSER_USE_MIN_KEY_DELAY_MS (see _apply_browser_use_min_key_delay).
        "wait_between_actions": float(os.getenv("BROWSER_USE_WAIT_BETWEEN_ACTIONS", "0.2")),
        # Give the DOM/network a bit longer to settle before the next action (reduces flaky inputs).
        "minimum_wait_page_load_time": float(os.getenv("BROWSER_USE_MIN_PAGE_LOAD_WAIT_S", "0.4")),
        "wait_for_network_idle_page_load_time": float(os.getenv("BROWSER_USE_NETWORK_IDLE_WAIT_S", "0.85")),
    }
    try:
        sig = inspect.signature(Browser)
        if "keep_alive" in sig.parameters:
            # Keep the underlying browser session alive for VNC review.
            browser_kwargs["keep_alive"] = bool(keep_open and not headless)
    except Exception:
        # Best-effort: if signature introspection fails, proceed without keep_alive.
        pass

    browser = Browser(**browser_kwargs)

    recorder = LLMUsageRecorder()
    provider_name, model_name = _llm_provider_and_model_for_env()
    llm = TokenTrackingLLM(_make_agent_llm(), provider=provider_name, model=model_name, recorder=recorder)

    # Cooperative pause / takeover control. When the orchestrator writes
    # state.json into AGENT_CONTROL_DIR, the LLM wrappers see it via this
    # shared instance. The telemetry loop also reports the observed state
    # so the UI can show whether the agent is actually paused.
    agent_control = _get_agent_control()

    # Progressively report token usage/cost to the orchestrator so global cost updates mid-run.
    mid = (os.getenv("ORCH_MACHINE_ID") or "").strip()
    stop_telemetry = threading.Event()

    def _telemetry_loop() -> None:
        if not mid:
            return
        interval_s = float(os.getenv("ORCH_TELEMETRY_INTERVAL_S", "5.0") or 5.0)
        interval_s = max(1.0, min(interval_s, 60.0))
        while not stop_telemetry.is_set():
            try:
                totals = recorder.totals()
                payload: dict[str, Any] = {
                    "llm_tokens": totals.get("total_tokens"),
                    "llm_cost_usd": totals.get("estimated_cost_usd"),
                }
                # Refresh observed control state (without blocking) and include
                # it in the telemetry payload so the UI reflects reality.
                try:
                    payload["agent_state"] = agent_control.poll()
                except Exception:
                    pass
                _orch_post_json(
                    f"/api/machines/{mid}/telemetry",
                    payload,
                    timeout_s=2.0,
                )
            except Exception:
                pass
            stop_telemetry.wait(interval_s)

    telemetry_thread: threading.Thread | None = None
    if mid and (os.getenv("ORCH_API_BASE") or "").strip():
        telemetry_thread = threading.Thread(target=_telemetry_loop, daemon=True)
        telemetry_thread.start()

    # Per-step screenshot capture. Index is incremented on every successful post so
    # `NNNN.png` maps 1:1 to agent step order. Capped by ORCH_SCREENSHOT_MAX_PER_RUN.
    _step_shot_counter = {"i": 0}
    _SHOT_MAX = max(0, int(os.getenv("AGENT_SCREENSHOT_MAX_PER_RUN", "200") or 200))
    _SHOTS_ENABLED = (os.getenv("AGENT_SCREENSHOTS_ENABLED", "1") or "1").strip() not in ("0", "", "false", "no")

    async def _capture_screenshot_b64(agent_obj: Any) -> str | None:
        """
        Take a viewport (not full-page) screenshot of the current browser tab and
        return it as base64-encoded PNG. Used to record the state right after the
        agent fills a form field.
        """
        sess = getattr(agent_obj, "browser_session", None)
        if sess is None:
            return None
        # Prefer the high-level helper when available.
        take = getattr(sess, "take_screenshot", None)
        if callable(take):
            try:
                try:
                    params = inspect.signature(take).parameters
                except (TypeError, ValueError):
                    params = {}
                kw: dict[str, Any] = {}
                if "full_page" in params:
                    kw["full_page"] = False
                data = await take(**kw)
                if isinstance(data, (bytes, bytearray)):
                    return base64.b64encode(bytes(data)).decode("ascii")
                if isinstance(data, str):
                    if data.startswith("data:"):
                        c = data.find(",")
                        return data[c + 1 :] if c >= 0 else None
                    return data
            except Exception:
                pass
        # Fallback: drive the underlying Playwright page directly.
        for attr in ("get_current_page", "get_active_page", "get_page"):
            fn = getattr(sess, attr, None)
            if not callable(fn):
                continue
            try:
                page = await fn()
                data = await page.screenshot(full_page=False, type="png")
                if isinstance(data, (bytes, bytearray)):
                    return base64.b64encode(bytes(data)).decode("ascii")
            except Exception:
                continue
        return None

    # Serialize concurrent screenshot captures so a rapid sequence of field fills
    # doesn't try to drive Playwright in parallel.
    _shot_lock = asyncio.Lock()

    async def _send_field_screenshot(agent_obj: Any, field_hint: str = "") -> None:
        """
        Take a viewport screenshot of the current page and upload it to the
        orchestrator. Intended to be called right after a form field is filled.
        """
        if not _SHOTS_ENABLED:
            return
        mid = (os.getenv("ORCH_MACHINE_ID") or "").strip()
        base = (os.getenv("ORCH_API_BASE") or "").strip()
        if not mid or not base:
            return
        async with _shot_lock:
            idx = _step_shot_counter["i"]
            if idx >= _SHOT_MAX:
                return
            b64 = await _capture_screenshot_b64(agent_obj)
            if not b64:
                return
            page_url = ""
            try:
                sess = getattr(agent_obj, "browser_session", None)
                for attr in ("get_current_page", "get_active_page", "get_page"):
                    fn = getattr(sess, attr, None) if sess else None
                    if not callable(fn):
                        continue
                    page = await fn()
                    page_url = str(getattr(page, "url", "") or "")
                    if page_url:
                        break
            except Exception:
                page_url = ""
            if not page_url:
                try:
                    hist = getattr(agent_obj, "history", None)
                    items = getattr(hist, "history", None) if hist is not None else None
                    if items:
                        state = getattr(items[-1], "state", None)
                        if state is not None:
                            page_url = str(getattr(state, "url", "") or "")
                except Exception:
                    pass
            payload = {
                "run_id": run_id,
                "step_index": idx,
                "image_b64": b64,
                "page_url": page_url,
                "field": (field_hint or "").strip()[:200],
            }
            loop = asyncio.get_event_loop()

            def _post() -> bool:
                return _orch_post_json(
                    f"/api/machines/{mid}/screenshot", payload, timeout_s=8.0
                )

            try:
                ok = await loop.run_in_executor(None, _post)
            except Exception:
                ok = False
            if ok:
                _step_shot_counter["i"] = idx + 1

    agent = Agent(
        task=_build_task(job_url, profile),
        llm=llm,
        page_extraction_llm=llm,
        # Large step timeout so interactive prompts can wait indefinitely.
        # Browser-Use requires an int; set to 24h.
        step_timeout=int(os.getenv("AGENT_STEP_TIMEOUT", "86400")),
        browser=browser,
        tools=tools,
        available_file_paths=available_files,
    )
    agent_box["agent"] = agent

    _install_field_highlighter(
        agent_box=agent_box,
        loop=asyncio.get_running_loop(),
        profiler=profiler,
    )

    status = "Failed"
    description = ""
    final: str | None = None
    judge_info: dict[str, Any] | None = None
    self_reported_success: bool | None = None

    def _install_typed_value_tracker() -> Callable[[], None] | None:
        """
        Wrap browser-use's internal text-input methods so every typed string is
        recorded in `typed_values` and a viewport screenshot is posted to the
        orchestrator right after the field is filled. Returns a restore callable,
        or None on failure.
        """
        try:
            from browser_use.browser.watchdogs.default_action_watchdog import (  # type: ignore
                DefaultActionWatchdog,
            )
        except Exception:
            return None

        def _extract_text(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
            """Record all strings found in args/kwargs and return the first
            non-empty candidate so we can tag the resulting screenshot."""
            candidates: list[Any] = list(args) + list(kwargs.values())
            first_text: str = ""
            for c in candidates:
                if isinstance(c, str):
                    if c:
                        typed_values.append(c)
                        if not first_text:
                            first_text = c
                    continue
                t = getattr(c, "text", None)
                if isinstance(t, str) and t:
                    typed_values.append(t)
                    if not first_text:
                        first_text = t
            return first_text

        def _schedule_shot(hint: str) -> None:
            a = agent_box.get("agent")
            if a is None:
                return
            try:
                asyncio.create_task(_send_field_screenshot(a, hint))
            except RuntimeError:
                # No running event loop (shouldn't happen inside the agent run,
                # but stay safe and silently drop the shot).
                pass

        prev_input = getattr(DefaultActionWatchdog, "_input_text_element_node_impl", None)
        prev_page = getattr(DefaultActionWatchdog, "_type_to_page", None)

        if prev_input is None and prev_page is None:
            return None

        async def _tracked_input(self, *args: Any, **kwargs: Any) -> Any:
            hint = ""
            try:
                hint = _extract_text(args, kwargs)
            except Exception:
                pass
            result = await prev_input(self, *args, **kwargs)
            try:
                _schedule_shot(hint)
            except Exception:
                pass
            return result

        async def _tracked_page(self, *args: Any, **kwargs: Any) -> Any:
            hint = ""
            try:
                hint = _extract_text(args, kwargs)
            except Exception:
                pass
            result = await prev_page(self, *args, **kwargs)
            try:
                _schedule_shot(hint)
            except Exception:
                pass
            return result

        if prev_input is not None:
            DefaultActionWatchdog._input_text_element_node_impl = _tracked_input  # type: ignore[assignment]
        if prev_page is not None:
            DefaultActionWatchdog._type_to_page = _tracked_page  # type: ignore[assignment]

        def _restore() -> None:
            if prev_input is not None:
                DefaultActionWatchdog._input_text_element_node_impl = prev_input  # type: ignore[assignment]
            if prev_page is not None:
                DefaultActionWatchdog._type_to_page = prev_page  # type: ignore[assignment]

        return _restore

    _restore_tracker = _install_typed_value_tracker()

    try:
        run_kw: dict[str, Any] = {"max_steps": max_steps}
        history = await agent.run(**run_kw)
        try:
            final = history.final_result()
        except Exception:
            final = None
        try:
            self_reported_success = history.is_successful()
        except Exception:
            self_reported_success = None
        try:
            judge_info = history.judgement()
        except Exception:
            judge_info = None
        if final:
            description = str(final).strip()
            low = description.lower()
            if "not found" in low or "404" in low:
                status = "Not found"
            else:
                status = "Finished"

        # Reconcile self-reported success and the judge verdict.
        # The previous code only looked at the final_result() text, so a run
        # where the agent admitted it did NOT click submit (e.g. user declined
        # pre-submit confirmation) was still recorded as "Finished" / success
        # in the orchestrator UI, contradicting the judge's ❌ FAIL verdict.
        judge_verdict: bool | None = None
        judge_failure_reason: str = ""
        if isinstance(judge_info, dict):
            v = judge_info.get("verdict")
            if isinstance(v, bool):
                judge_verdict = v
            fr = judge_info.get("failure_reason")
            if isinstance(fr, str):
                judge_failure_reason = fr.strip()

        # "Not found" is a legitimate terminal state for the agent (the job
        # listing is gone) and should not be downgraded to "Failed".
        if status != "Not found":
            submit_was_confirmed = any(
                bool(v) for v in submit_confirmed_once.values()
            )
            relative_fields_present = getattr(
                profiler, "relative_used_in_current_form", False
            )
            requires_manual_submit = bool(relative_fields_present)

            agent_failed = self_reported_success is False
            judge_failed = judge_verdict is False
            # If the agent ended but the human-gated submit was never
            # actually confirmed while there were relative fields that
            # required it, the form was almost certainly not submitted.
            submission_skipped = requires_manual_submit and not submit_was_confirmed

            if agent_failed or judge_failed or submission_skipped:
                status = "Failed"
                # Enrich the persisted description so the UI explains WHY the
                # run was marked as failed, instead of only echoing the agent's
                # upbeat "form is prepared" message.
                reason_bits: list[str] = []
                if judge_failed and judge_failure_reason:
                    reason_bits.append(f"Judge: {judge_failure_reason}")
                elif judge_failed:
                    reason_bits.append("Judge verdict: FAIL")
                if submission_skipped and "submit" not in " ".join(reason_bits).lower():
                    reason_bits.append(
                        "Application was not submitted: the pre-submit "
                        "confirmation was declined or never clicked."
                    )
                if reason_bits:
                    note = " ".join(reason_bits).strip()
                    description = (
                        f"{description}\n\n[Verdict: FAIL] {note}"
                        if description
                        else f"[Verdict: FAIL] {note}"
                    )
    except Exception as e:
        description = f"{type(e).__name__}: {e}".strip()
        # Detect "out of funds / insufficient quota" and surface it to the orchestrator UI.
        try:
            msg = (str(e) or "").lower()
            if "insufficient_quota" in msg or "exceeded your current quota" in msg or "check your plan and billing" in msg:
                mmid = (os.getenv("ORCH_MACHINE_ID") or "").strip()
                if mmid:
                    _orch_post_json(
                        f"/api/machines/{mmid}/attention",
                        {"needed": True, "reason": "out_of_funds"},
                        timeout_s=2.0,
                    )
        except Exception:
            pass
        status = "Failed"
    finally:
        stop_telemetry.set()
        if _restore_tracker is not None:
            try:
                _restore_tracker()
            except Exception:
                pass

    llm_totals = recorder.totals()
    llm_breakdown = recorder.snapshot()

    # Persist run record (best-effort) back to orchestrator (host)
    def _persist_result_with_llm(status: str, description: str) -> None:
        mid = (os.getenv("ORCH_MACHINE_ID") or "").strip()
        if not mid:
            return
        payload: dict[str, Any] = {
            "run_id": run_id,
            "application_url": job_url,
            "status": status,
            "description": description,
            "fields": _form_fields_only(),
            "profile_id": profile_id,
            "llm_model": (os.getenv("OPENAI_MODEL") or "").strip(),
            "llm": {
                "totals": llm_totals,
                "by_model": llm_breakdown,
            },
            "llm_tokens": llm_totals.get("total_tokens"),
            "llm_cost_usd": llm_totals.get("estimated_cost_usd"),
            "self_reported_success": self_reported_success,
            "judge": judge_info,
            "submitted": any(bool(v) for v in submit_confirmed_once.values()),
        }
        _orch_post_json(
            f"/api/machines/{mid}/application-result",
            payload,
            timeout_s=6.0,
        )

    _persist_result_with_llm(status, description)

    if final:
        print("\n=== Final result ===\n")
        print(final)
    elif description:
        print("\n=== Final result ===\n")
        print(description)

    if llm_totals.get("total_tokens") or llm_totals.get("estimated_cost_usd") is not None:
        print("\n=== LLM usage (est.) ===\n")
        print(
            f"Tokens: in={llm_totals.get('input_tokens') or 0} "
            f"out={llm_totals.get('output_tokens') or 0} "
            f"total={llm_totals.get('total_tokens') or 0}"
        )
        if llm_totals.get("estimated_cost_usd") is None:
            print("Cost: unknown (no pricing for this model; set AGENT_LLM_PRICING_JSON to override)")
        else:
            print(f"Cost: ${float(llm_totals['estimated_cost_usd']):.4f}")

    if keep_open and not headless:
        # Keep the browser window (VNC) open for review until the user stops the machine.
        print("\nBrowser will remain open. Stop the machine when you're done reviewing.\n")
        while True:
            time.sleep(3600)

    if status == "Not found":
        return 2
    if status != "Finished":
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    # When running under the orchestrator, stdin is not a reliable control plane and
    # waiting for Enter can look like the machine is "stuck loading".
    # Keep the prompt for local interactive runs only.
    if not (os.getenv("ORCH_MACHINE_ID") or "").strip():
        _input_with_periodic_bell("Press Enter to START...")

    p = argparse.ArgumentParser(
        prog="agent",
        description="Interactive Browser-Use agent to fill out job applications from a URL using a stored applicant profile.",
    )
    p.add_argument("--url", required=True, help="Job application URL to open.")

    prof = p.add_mutually_exclusive_group(required=True)
    prof.add_argument("--profile", help="Profile as JSON string OR path to JSON file.")
    prof.add_argument("--db-profile", action="store_true", help="Load profile from profiles_db.json via --applicant-id/--profile-id.")

    p.add_argument("--db", default="profiles_db.json", help="Path to profiles DB JSON (only with --db-profile).")
    p.add_argument("--profile-id", default=None, help="Profile id (only with --db-profile).")

    p.add_argument("--max-steps", type=int, default=60, help="Maximum agent steps.")
    p.add_argument("--headless", action="store_true", help="Run browser in headless mode (not recommended for manual review).")
    p.add_argument(
        "--keep-open",
        action="store_true",
        help="Keep the browser open after the agent finishes (useful for review in VNC).",
    )
    p.add_argument(
        "--browser-binary",
        default=None,
        help="Path to Chrome/Chromium executable. If not set, auto-detects common locations.",
    )
    p.add_argument(
        "--allow-file",
        action="append",
        default=[],
        help="Absolute path to a file the agent may read/upload (repeatable).",
    )

    args = p.parse_args(argv)

    _load_dotenv()
    _set_default_timeouts()
    if args.browser_binary:
        os.environ["BROWSER_USE_BROWSER_BINARY"] = args.browser_binary

    if args.db_profile:
        if not args.profile_id:
            raise SystemExit("--db-profile requires --profile-id")
        profile_obj = _load_profile_from_db(args.db, args.profile_id)
    else:
        raise SystemExit("This agent requires a stored profile. Use --db-profile with --profile-id.")

    if not isinstance(profile_obj, dict):
        raise SystemExit("Profile must be a JSON object.")

    profile_obj = _ensure_minimum_profile_fields(profile_obj)
    available_files = _collect_available_file_paths(profile_obj, list(args.allow_file))
    # If the profile lists attachments but none of them resolved, give the user a clear hint.
    att = _attachments_dir()
    profile_attachments = profile_obj.get("attachments")
    has_doc_refs = isinstance(profile_attachments, dict) and any(
        isinstance(v, str) and v.strip() for v in profile_attachments.values()
    )
    if has_doc_refs and not available_files:
        hint = (
            "Note: your profile references attachment file(s), but none were found on disk.\n"
            "Upload them from the Profiles page in the orchestrator UI; they land under\n"
            "`attachments/<profile_id>/`. The agent mounts that folder read-only at runtime.\n"
        )
        if att:
            hint += f"Attachments directory in use: {att}\n"
        print(hint)

    try:
        rc = asyncio.run(
            _run(
                args.url,
                profile_obj,
                max_steps=args.max_steps,
                headless=bool(args.headless),
                available_files=available_files,
                db_path=args.db,
                profile_id=args.profile_id,
                keep_open=bool(args.keep_open) or bool((os.getenv("ORCH_MACHINE_ID") or "").strip()),
            )
        )
    except TakeoverRequested as exc:
        # Orchestrator asked us to stop so the human can take over. Print a
        # clean message, report the terminal state to the orchestrator if
        # possible, and exit with the dedicated takeover code so the
        # container's tmux session logs it distinctly.
        print(f"\n[agent] {exc}. Leaving the desktop for human takeover.\n")
        _mid = (os.getenv("ORCH_MACHINE_ID") or "").strip()
        if _mid:
            try:
                _orch_post_json(
                    f"/api/machines/{_mid}/telemetry",
                    {"agent_state": "stopping"},
                    timeout_s=2.0,
                )
            except Exception:
                pass
        return int(exc.code or 0)
    return int(rc)


if __name__ == "__main__":
    raise SystemExit(main())

