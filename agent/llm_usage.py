"""
Accurate LLM token accounting and cost calculation.

Design goals
------------
* Record the exact token counts reported by the provider (never re-tokenize).
* Break usage down into the sub-dimensions the provider actually bills on:
  input, cached-input, output, reasoning (counted as output), audio in/out.
* Support multiple pricing tiers per model (standard, batch, cached, finetune).
* Recognize OpenAI fine-tune IDs (``ft:<base>:org::id``) and map to their base.
* Persist a JSONL ledger row per call so historical cost can be recomputed or
  reconciled against OpenAI's Usage/Costs API.
* Keep backwards-compatible with older callers that only pass input/output.

Public API (stable)
-------------------
* ``TokenUsage`` dataclass
* ``ModelPricing`` dataclass
* ``extract_token_usage(obj) -> TokenUsage | None``
* ``estimate_cost_usd(*, model, usage, tier="standard") -> float | None``
* ``LLMUsageRecorder`` with ``.add``, ``.snapshot``, ``.totals``
* ``TokenTrackingLLM`` wrapper for LangChain-style ``invoke``/``ainvoke``
* ``TrackedOpenAI`` wrapper around an ``openai.OpenAI`` client that records
  usage on ``chat.completions.create``, ``responses.create`` and
  ``embeddings.create`` (and auto-requests ``stream_options={include_usage}``).

Environment
-----------
* ``AGENT_LLM_PRICING_FILE``   – path to a JSON file with pricing overrides.
* ``AGENT_LLM_PRICING_JSON``   – inline JSON overrides (takes precedence).
* ``AGENT_LLM_PRICING_MAX_AGE_DAYS`` – warn if pricing file is older (default 45).
* ``AGENT_LLM_FINETUNE_MULTIPLIER`` – fallback price multiplier for fine-tune
  models when no explicit price is configured (default 2.0).
* ``AGENT_LLM_LEDGER_PATH``    – append-only JSONL ledger of individual calls.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

_logger = logging.getLogger(__name__)

_USER_GUIDANCE_REL = "user_guidance.txt"


def _operator_guidance_text(control: Any) -> str:
    """Return trimmed text from ``user_guidance.txt`` next to ``state.json``, if any."""
    try:
        sp = getattr(control, "state_path", None)
        if sp is None:
            return ""
        path = Path(sp).parent / _USER_GUIDANCE_REL
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""


def _system_guidance_message(text: str) -> Any:
    """
    Build a system message object compatible with the LLM stack in use.

    browser-use passes ``browser_use.llm.messages.*`` (Pydantic models), not
    LangChain's ``langchain_core.messages`` — LangChain ``SystemMessage`` instances
    fail ``isinstance`` checks and break serializers, so guidance was never applied.
    """
    try:
        from browser_use.llm.messages import SystemMessage as BU_SystemMessage

        return BU_SystemMessage(content=text)
    except Exception:
        pass
    try:
        from langchain_core.messages import SystemMessage as LC_SystemMessage

        return LC_SystemMessage(content=text)
    except Exception:
        return None


def _looks_like_llm_message_list(msgs: Any) -> bool:
    if not isinstance(msgs, list) or not msgs:
        return False
    first = msgs[0]
    return hasattr(first, "role") and hasattr(first, "content")


def _inject_operator_guidance_messages(
    args: Tuple[Any, ...], kwargs: Dict[str, Any], body: str
) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
    """Prepend a system message to the chat history passed into the model."""
    if not body:
        return args, kwargs
    prefix = (
        "Operator guidance (orchestrator UI). Prefer following this unless it conflicts "
        "with safety or policy:\n\n"
    )
    msg = _system_guidance_message(prefix + body)
    if msg is None:
        return args, kwargs
    kw = dict(kwargs)
    m = kw.get("messages")
    if isinstance(m, list) and m and _looks_like_llm_message_list(m):
        kw["messages"] = [msg, *m]
        return args, kw
    if args and isinstance(args[0], list) and args[0] and _looks_like_llm_message_list(args[0]):
        new_first = [msg, *args[0]]
        return (new_first,) + args[1:], kw
    return args, kw

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

# Pricing tiers supported by the recorder. "standard" is the list price.
# "batch" is half price per OpenAI Batch API rules. "finetune" reads from a
# dedicated entry or falls back to `input/output * AGENT_LLM_FINETUNE_MULTIPLIER`.
Tier = str
TIER_STANDARD: Tier = "standard"
TIER_BATCH: Tier = "batch"
TIER_FINETUNE: Tier = "finetune"

_BATCH_DISCOUNT = 0.5


@dataclass
class TokenUsage:
    """
    Per-call token usage.

    Semantics (follow OpenAI's own shape):
    * ``input_tokens`` is the full prompt size, INCLUDING ``cached_input_tokens``
      and ``audio_input_tokens`` if any.
    * ``output_tokens`` is the full completion size, INCLUDING ``reasoning_tokens``
      and ``audio_output_tokens`` if any.
    Cost math subtracts the sub-fields out so each is billed at the right rate.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    audio_input_tokens: int = 0
    audio_output_tokens: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.input_tokens += int(other.input_tokens or 0)
        self.output_tokens += int(other.output_tokens or 0)
        self.total_tokens = int(self.total_tokens or 0) + int(other.total_tokens or 0)
        self.cached_input_tokens += int(other.cached_input_tokens or 0)
        self.reasoning_tokens += int(other.reasoning_tokens or 0)
        self.audio_input_tokens += int(other.audio_input_tokens or 0)
        self.audio_output_tokens += int(other.audio_output_tokens or 0)

    def normalized(self) -> "TokenUsage":
        it = max(0, int(self.input_tokens or 0))
        ot = max(0, int(self.output_tokens or 0))
        tt = max(0, int(self.total_tokens or 0))
        ci = max(0, int(self.cached_input_tokens or 0))
        rt = max(0, int(self.reasoning_tokens or 0))
        ai = max(0, int(self.audio_input_tokens or 0))
        ao = max(0, int(self.audio_output_tokens or 0))

        # Clamp sub-fields to their parent totals to guard against bad inputs.
        ci = min(ci, it)
        ai = min(ai, max(0, it - ci))
        rt = min(rt, ot)
        ao = min(ao, max(0, ot - rt))

        if tt <= 0 and (it > 0 or ot > 0):
            tt = it + ot
        return TokenUsage(
            input_tokens=it,
            output_tokens=ot,
            total_tokens=tt,
            cached_input_tokens=ci,
            reasoning_tokens=rt,
            audio_input_tokens=ai,
            audio_output_tokens=ao,
        )

    def to_dict(self) -> dict[str, int]:
        u = self.normalized()
        return {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "total_tokens": u.total_tokens,
            "cached_input_tokens": u.cached_input_tokens,
            "reasoning_tokens": u.reasoning_tokens,
            "audio_input_tokens": u.audio_input_tokens,
            "audio_output_tokens": u.audio_output_tokens,
        }


@dataclass
class ModelPricing:
    """
    USD per 1M tokens. Sub-rates default to `None`, which means "same as the
    base input/output rate" (i.e. no special discount or surcharge applied).
    Set them explicitly to model provider-specific rules like cached input.
    """

    usd_per_1m_input: float
    usd_per_1m_output: float
    usd_per_1m_cached_input: Optional[float] = None
    usd_per_1m_audio_input: Optional[float] = None
    usd_per_1m_audio_output: Optional[float] = None


# ---------------------------------------------------------------------------
# Pricing catalog
# ---------------------------------------------------------------------------

def _default_pricing_table() -> Dict[str, ModelPricing]:
    """
    Default token pricing in USD per 1M tokens.

    Notes
    -----
    * These are provider list prices; Batch and fine-tune tiers are derived
      automatically (see :func:`_apply_tier`).
    * OpenAI's cached-input rate is set explicitly where published, not derived.
    * If a model isn't listed, configure it via ``AGENT_LLM_PRICING_FILE`` /
      ``AGENT_LLM_PRICING_JSON`` rather than editing this file.
    """
    return {
        # OpenAI — https://platform.openai.com/docs/pricing
        # gpt-4.1 family: cached-input is 25% of input.
        "gpt-4.1": ModelPricing(
            usd_per_1m_input=2.00,
            usd_per_1m_output=8.00,
            usd_per_1m_cached_input=0.50,
        ),
        "gpt-4.1-mini": ModelPricing(
            usd_per_1m_input=0.40,
            usd_per_1m_output=1.60,
            usd_per_1m_cached_input=0.10,
        ),
        "gpt-4.1-nano": ModelPricing(
            usd_per_1m_input=0.10,
            usd_per_1m_output=0.40,
            usd_per_1m_cached_input=0.025,
        ),
        # gpt-5.4 family: provisional placeholders (agent/pricing.json has the
        # authoritative numbers once the provider publishes them). Values are
        # rough extrapolations from the 4.1/4o tiers so cost reports aren't
        # "unknown" when this family is used.
        "gpt-5.4": ModelPricing(
            usd_per_1m_input=2.50,
            usd_per_1m_output=10.00,
            usd_per_1m_cached_input=0.625,
        ),
        "gpt-5.4-mini": ModelPricing(
            usd_per_1m_input=0.50,
            usd_per_1m_output=2.00,
            usd_per_1m_cached_input=0.125,
        ),
        "gpt-5.4-nano": ModelPricing(
            usd_per_1m_input=0.12,
            usd_per_1m_output=0.50,
            usd_per_1m_cached_input=0.030,
        ),
        # gpt-4o family: cached-input is 50% of input.
        "gpt-4o": ModelPricing(
            usd_per_1m_input=2.50,
            usd_per_1m_output=10.00,
            usd_per_1m_cached_input=1.25,
        ),
        "gpt-4o-mini": ModelPricing(
            usd_per_1m_input=0.15,
            usd_per_1m_output=0.60,
            usd_per_1m_cached_input=0.075,
        ),
        # o-series reasoning models. Keep conservative defaults; override in
        # AGENT_LLM_PRICING_FILE once pinned to a specific dated SKU.
        "o4-mini": ModelPricing(
            usd_per_1m_input=1.10,
            usd_per_1m_output=4.40,
            usd_per_1m_cached_input=0.275,
        ),
        "o3": ModelPricing(
            usd_per_1m_input=2.00,
            usd_per_1m_output=8.00,
            usd_per_1m_cached_input=0.50,
        ),
        "o3-mini": ModelPricing(
            usd_per_1m_input=1.10,
            usd_per_1m_output=4.40,
            usd_per_1m_cached_input=0.55,
        ),
        "o1": ModelPricing(
            usd_per_1m_input=15.00,
            usd_per_1m_output=60.00,
            usd_per_1m_cached_input=7.50,
        ),
        "o1-mini": ModelPricing(
            usd_per_1m_input=1.10,
            usd_per_1m_output=4.40,
            usd_per_1m_cached_input=0.55,
        ),
        # Embeddings (output tokens are always 0; priced per input only).
        "text-embedding-3-small": ModelPricing(usd_per_1m_input=0.02, usd_per_1m_output=0.0),
        "text-embedding-3-large": ModelPricing(usd_per_1m_input=0.13, usd_per_1m_output=0.0),
        "text-embedding-ada-002": ModelPricing(usd_per_1m_input=0.10, usd_per_1m_output=0.0),
        # Anthropic — https://docs.anthropic.com/en/docs/about-claude/pricing
        "claude-opus-4-6": ModelPricing(usd_per_1m_input=5.00, usd_per_1m_output=25.00),
        "claude-opus-4-5": ModelPricing(usd_per_1m_input=5.00, usd_per_1m_output=25.00),
        # Google Gemini — https://ai.google.dev/gemini-api/docs/pricing
        "gemini-flash-latest": ModelPricing(usd_per_1m_input=0.10, usd_per_1m_output=0.40),
        # DeepSeek — https://api-docs.deepseek.com/quick_start/pricing
        # Cached-input pricing = cache hit; non-cached input = "cache miss" rate.
        # Refresh from agent/pricing.json after each provider price change.
        "deepseek-v4-flash": ModelPricing(
            usd_per_1m_input=0.14,
            usd_per_1m_output=0.28,
            usd_per_1m_cached_input=0.028,
        ),
        "deepseek-v4-pro": ModelPricing(
            usd_per_1m_input=1.74,
            usd_per_1m_output=3.48,
            usd_per_1m_cached_input=0.145,
        ),
        "deepseek-chat": ModelPricing(
            usd_per_1m_input=0.27,
            usd_per_1m_output=1.10,
            usd_per_1m_cached_input=0.07,
        ),
        "deepseek-reasoner": ModelPricing(
            usd_per_1m_input=0.55,
            usd_per_1m_output=2.19,
            usd_per_1m_cached_input=0.14,
        ),
        # Browser-Use — https://browser-use.com/changelog
        "bu-2-0": ModelPricing(usd_per_1m_input=0.60, usd_per_1m_output=3.50),
        "bu-1-0": ModelPricing(usd_per_1m_input=0.20, usd_per_1m_output=2.00),
    }


# Cache the resolved pricing table so we only parse overrides once per process
# unless a file-based override changes on disk.
_PRICING_LOCK = threading.Lock()
_PRICING_CACHE: Dict[str, ModelPricing] | None = None
_PRICING_FILE_MTIME: float | None = None
_PRICING_WARN_EMITTED = False


def _load_pricing_file(path: str) -> tuple[Dict[str, ModelPricing], Optional[datetime]]:
    global _PRICING_WARN_EMITTED
    try:
        p = Path(path)
        raw = p.read_text(encoding="utf-8")
        doc = json.loads(raw)
    except Exception as exc:
        _logger.warning("llm_usage: failed to read pricing file %s: %s", path, exc)
        return {}, None

    pricing_map: Dict[str, ModelPricing] = {}
    models = doc.get("models") if isinstance(doc, dict) else None
    if not isinstance(models, dict):
        # Allow a flat top-level map as a simpler format.
        models = doc if isinstance(doc, dict) else {}

    for k, v in models.items():
        if not isinstance(k, str) or not isinstance(v, dict):
            continue
        ip = v.get("usd_per_1m_input")
        op = v.get("usd_per_1m_output")
        if not (isinstance(ip, (int, float)) and isinstance(op, (int, float))):
            continue
        pricing_map[_normalize_model_name(k) or k.strip()] = ModelPricing(
            usd_per_1m_input=float(ip),
            usd_per_1m_output=float(op),
            usd_per_1m_cached_input=_opt_float(v.get("usd_per_1m_cached_input")),
            usd_per_1m_audio_input=_opt_float(v.get("usd_per_1m_audio_input")),
            usd_per_1m_audio_output=_opt_float(v.get("usd_per_1m_audio_output")),
        )

    last_updated: Optional[datetime] = None
    if isinstance(doc, dict):
        lu = doc.get("last_updated")
        if isinstance(lu, str):
            try:
                last_updated = datetime.fromisoformat(lu.replace("Z", "+00:00"))
            except ValueError:
                last_updated = None

    if last_updated is not None:
        max_age = float(os.getenv("AGENT_LLM_PRICING_MAX_AGE_DAYS", "45") or 45.0)
        age_days = (datetime.now(UTC) - (last_updated if last_updated.tzinfo else last_updated.replace(tzinfo=UTC))).days
        if age_days > max_age and not _PRICING_WARN_EMITTED:
            _logger.warning(
                "llm_usage: pricing file %s is %d days old (> %.0f). "
                "Costs may be inaccurate — refresh from the provider pricing pages.",
                path,
                age_days,
                max_age,
            )
            _PRICING_WARN_EMITTED = True

    return pricing_map, last_updated


def _opt_float(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) else None


def _resolved_pricing_table() -> Dict[str, ModelPricing]:
    global _PRICING_CACHE, _PRICING_FILE_MTIME
    with _PRICING_LOCK:
        file_path = (os.getenv("AGENT_LLM_PRICING_FILE") or "").strip()
        inline = (os.getenv("AGENT_LLM_PRICING_JSON") or "").strip()

        file_mtime: Optional[float] = None
        if file_path:
            try:
                file_mtime = Path(file_path).stat().st_mtime
            except OSError:
                file_mtime = None

        # Rebuild cache if first access, inline var present (cheap to re-parse),
        # or pricing file's mtime changed.
        if _PRICING_CACHE is None or inline or file_mtime != _PRICING_FILE_MTIME:
            table = _default_pricing_table()
            if file_path:
                file_map, _ = _load_pricing_file(file_path)
                table.update(file_map)
                _PRICING_FILE_MTIME = file_mtime
            if inline:
                try:
                    overrides = json.loads(inline)
                    if isinstance(overrides, dict):
                        for k, v in overrides.items():
                            if not isinstance(k, str) or not isinstance(v, dict):
                                continue
                            ip = v.get("usd_per_1m_input")
                            op = v.get("usd_per_1m_output")
                            if not (isinstance(ip, (int, float)) and isinstance(op, (int, float))):
                                continue
                            table[_normalize_model_name(k) or k.strip()] = ModelPricing(
                                usd_per_1m_input=float(ip),
                                usd_per_1m_output=float(op),
                                usd_per_1m_cached_input=_opt_float(v.get("usd_per_1m_cached_input")),
                                usd_per_1m_audio_input=_opt_float(v.get("usd_per_1m_audio_input")),
                                usd_per_1m_audio_output=_opt_float(v.get("usd_per_1m_audio_output")),
                            )
                except Exception as exc:
                    _logger.warning("llm_usage: ignoring bad AGENT_LLM_PRICING_JSON: %s", exc)
            _PRICING_CACHE = table
        return _PRICING_CACHE


# ---------------------------------------------------------------------------
# Model name normalization
# ---------------------------------------------------------------------------

def _strip_dated_suffix(name: str) -> str:
    """``gpt-4.1-2025-04-14`` -> ``gpt-4.1``. Leaves other names unchanged."""
    parts = name.split("-")
    if len(parts) >= 5 and parts[-3].isdigit() and parts[-2].isdigit() and parts[-1].isdigit():
        return "-".join(parts[:-3])
    return name


def _normalize_model_name(model: str | None) -> str:
    m = (model or "").strip()
    if not m:
        return ""
    # Strip optional provider prefix like "openai/gpt-4.1".
    base = m.split("/", 1)[-1]

    # Fine-tune IDs: "ft:<base>:<org>::<id>" -> keep the <base> segment.
    if base.startswith("ft:"):
        try:
            base = base.split(":", 2)[1] or base
        except IndexError:
            pass
    if base.startswith(("gpt-", "claude-")):
        base = _strip_dated_suffix(base)
    return base


def _parse_fine_tune(model: str | None) -> Optional[str]:
    """Return the base-model part of a fine-tune id, or None if not a fine-tune."""
    m = (model or "").strip()
    if not m.startswith("ft:"):
        return None
    try:
        base = m.split(":", 2)[1]
    except IndexError:
        return None
    return _normalize_model_name(base)


def pricing_for_model(model: str | None) -> Optional[ModelPricing]:
    key = _normalize_model_name(model)
    if not key:
        return None
    return _resolved_pricing_table().get(key)


# ---------------------------------------------------------------------------
# Tier-aware cost calculation
# ---------------------------------------------------------------------------

def _scale(pricing: ModelPricing, factor: float) -> ModelPricing:
    return ModelPricing(
        usd_per_1m_input=pricing.usd_per_1m_input * factor,
        usd_per_1m_output=pricing.usd_per_1m_output * factor,
        usd_per_1m_cached_input=(
            pricing.usd_per_1m_cached_input * factor
            if pricing.usd_per_1m_cached_input is not None
            else None
        ),
        usd_per_1m_audio_input=(
            pricing.usd_per_1m_audio_input * factor
            if pricing.usd_per_1m_audio_input is not None
            else None
        ),
        usd_per_1m_audio_output=(
            pricing.usd_per_1m_audio_output * factor
            if pricing.usd_per_1m_audio_output is not None
            else None
        ),
    )


def _apply_tier(pricing: ModelPricing, tier: Tier, *, is_fine_tune: bool = False) -> ModelPricing:
    """
    Return a :class:`ModelPricing` adjusted for the tier and fine-tune status.

    * ``standard``: base rate, fine-tune multiplier if applicable.
    * ``batch``: 50% off base (Batch API). Composes with fine-tune.
    * ``finetune``: fine-tune multiplier applied to the base rate.

    Fine-tune and Batch compose multiplicatively because OpenAI's Batch API
    accepts fine-tuned models and the 50% Batch discount applies on top of
    the fine-tune inference rate.
    """
    factor = 1.0
    if is_fine_tune or tier == TIER_FINETUNE:
        factor *= float(os.getenv("AGENT_LLM_FINETUNE_MULTIPLIER", "2.0") or 2.0)
    if tier == TIER_BATCH:
        factor *= _BATCH_DISCOUNT
    if factor == 1.0:
        return pricing
    return _scale(pricing, factor)


def estimate_cost_usd(
    *,
    model: str | None,
    usage: TokenUsage,
    tier: Tier = TIER_STANDARD,
) -> Optional[float]:
    """
    Compute the USD cost of a call, breaking the billable dimensions apart so
    cached/audio/reasoning tokens are billed at the right rate.

    Reasoning tokens are *already included* in ``output_tokens`` (per OpenAI)
    and are billed at the output rate, so no separate reasoning price is needed.
    """
    is_ft = isinstance(model, str) and model.startswith("ft:")

    pricing = pricing_for_model(model)
    if not pricing and is_ft:
        ft_base = _parse_fine_tune(model)
        if ft_base:
            pricing = _resolved_pricing_table().get(ft_base)
    if not pricing:
        return None

    pricing = _apply_tier(pricing, tier, is_fine_tune=is_ft)
    u = usage.normalized()

    # Split the input pool into (cached, audio, plain).
    plain_input = max(0, u.input_tokens - u.cached_input_tokens - u.audio_input_tokens)
    # Split the output pool into (audio, non-audio). Reasoning is a subset of
    # non-audio output and is billed at the same output rate, so no subtraction.
    plain_output = max(0, u.output_tokens - u.audio_output_tokens)

    cached_rate = (
        pricing.usd_per_1m_cached_input
        if pricing.usd_per_1m_cached_input is not None
        else pricing.usd_per_1m_input
    )
    audio_in_rate = (
        pricing.usd_per_1m_audio_input
        if pricing.usd_per_1m_audio_input is not None
        else pricing.usd_per_1m_input
    )
    audio_out_rate = (
        pricing.usd_per_1m_audio_output
        if pricing.usd_per_1m_audio_output is not None
        else pricing.usd_per_1m_output
    )

    cost = (
        plain_input * pricing.usd_per_1m_input
        + u.cached_input_tokens * cached_rate
        + u.audio_input_tokens * audio_in_rate
        + plain_output * pricing.usd_per_1m_output
        + u.audio_output_tokens * audio_out_rate
    ) / 1_000_000.0
    return float(cost)


# ---------------------------------------------------------------------------
# Usage extraction (from a provider response object)
# ---------------------------------------------------------------------------

def _int_or_zero(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _get(obj: Any, key: str) -> Any:
    """Attribute- or key-indexed read."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _extract_sub_tokens(usage: Any) -> tuple[int, int, int, int]:
    """
    Return (cached_input, reasoning, audio_input, audio_output) from a provider
    usage object. Handles both the OpenAI object form and dict form, and both
    Responses-API and Chat-Completions-API shapes.
    """
    cached = 0
    reasoning = 0
    audio_in = 0
    audio_out = 0

    prompt_details = _get(usage, "prompt_tokens_details") or _get(usage, "input_tokens_details")
    if prompt_details is not None:
        cached = _int_or_zero(_get(prompt_details, "cached_tokens"))
        audio_in = _int_or_zero(_get(prompt_details, "audio_tokens"))

    completion_details = _get(usage, "completion_tokens_details") or _get(usage, "output_tokens_details")
    if completion_details is not None:
        reasoning = _int_or_zero(_get(completion_details, "reasoning_tokens"))
        audio_out = _int_or_zero(_get(completion_details, "audio_tokens"))

    return cached, reasoning, audio_in, audio_out


def extract_token_usage(obj: Any) -> Optional[TokenUsage]:
    """
    Best-effort extraction of a :class:`TokenUsage` from common SDK response
    shapes: OpenAI (Responses + ChatCompletions), Anthropic, LangChain.
    """
    if obj is None:
        return None

    # 1) Provider SDKs expose `.usage` on the top-level response.
    usage = _get(obj, "usage")
    if usage is not None:
        it = _int_or_zero(_get(usage, "input_tokens") or _get(usage, "prompt_tokens"))
        ot = _int_or_zero(_get(usage, "output_tokens") or _get(usage, "completion_tokens"))
        tt = _int_or_zero(_get(usage, "total_tokens"))
        ci, rt, ai, ao = _extract_sub_tokens(usage)
        # browser_use ChatInvokeUsage + DeepSeek flat usage use top-level cache fields.
        if not ci:
            ci = _int_or_zero(
                _get(usage, "prompt_cached_tokens")
                or _get(usage, "prompt_cache_hit_tokens")
                or _get(usage, "cached_tokens")
            )
        if it or ot or tt:
            return TokenUsage(
                input_tokens=it,
                output_tokens=ot,
                total_tokens=tt,
                cached_input_tokens=ci,
                reasoning_tokens=rt,
                audio_input_tokens=ai,
                audio_output_tokens=ao,
            ).normalized()

    # 2) Bare usage object (no .usage wrapper).
    if (
        _get(obj, "prompt_tokens") is not None
        or _get(obj, "completion_tokens") is not None
        or _get(obj, "input_tokens") is not None
        or _get(obj, "output_tokens") is not None
    ):
        it = _int_or_zero(_get(obj, "prompt_tokens") or _get(obj, "input_tokens"))
        ot = _int_or_zero(_get(obj, "completion_tokens") or _get(obj, "output_tokens"))
        tt = _int_or_zero(_get(obj, "total_tokens"))
        ci, rt, ai, ao = _extract_sub_tokens(obj)
        if it or ot or tt:
            return TokenUsage(
                input_tokens=it,
                output_tokens=ot,
                total_tokens=tt,
                cached_input_tokens=ci,
                reasoning_tokens=rt,
                audio_input_tokens=ai,
                audio_output_tokens=ao,
            ).normalized()

    # 3) LangChain AIMessage: .usage_metadata / .response_metadata.
    for attr in ("usage_metadata", "response_metadata"):
        meta = getattr(obj, attr, None)
        if isinstance(meta, dict):
            u = _extract_from_dict(meta)
            if u is not None:
                return u
            tu = meta.get("token_usage")
            if isinstance(tu, dict):
                u = _extract_from_dict(tu)
                if u is not None:
                    return u

    # 4) Generic dict with `usage`.
    if isinstance(obj, dict):
        u_dict = obj.get("usage") if isinstance(obj.get("usage"), dict) else obj
        if isinstance(u_dict, dict):
            u = _extract_from_dict(u_dict)
            if u is not None:
                return u

    return None


def _extract_from_dict(d: dict) -> Optional[TokenUsage]:
    it = _int_or_zero(d.get("input_tokens") or d.get("prompt_tokens"))
    ot = _int_or_zero(d.get("output_tokens") or d.get("completion_tokens"))
    tt = _int_or_zero(d.get("total_tokens"))
    ci = 0
    rt = 0
    ai = 0
    ao = 0
    pd = d.get("prompt_tokens_details") or d.get("input_tokens_details")
    if isinstance(pd, dict):
        ci = _int_or_zero(pd.get("cached_tokens"))
        ai = _int_or_zero(pd.get("audio_tokens"))
    cd = d.get("completion_tokens_details") or d.get("output_tokens_details")
    if isinstance(cd, dict):
        rt = _int_or_zero(cd.get("reasoning_tokens"))
        ao = _int_or_zero(cd.get("audio_tokens"))
    # Also accept top-level flat sub-fields.
    ci = ci or _int_or_zero(d.get("cached_input_tokens") or d.get("cached_tokens"))
    ci = ci or _int_or_zero(d.get("prompt_cache_hit_tokens") or d.get("prompt_cached_tokens"))
    rt = rt or _int_or_zero(d.get("reasoning_tokens"))
    ai = ai or _int_or_zero(d.get("audio_input_tokens"))
    ao = ao or _int_or_zero(d.get("audio_output_tokens"))
    if it or ot or tt:
        return TokenUsage(
            input_tokens=it,
            output_tokens=ot,
            total_tokens=tt,
            cached_input_tokens=ci,
            reasoning_tokens=rt,
            audio_input_tokens=ai,
            audio_output_tokens=ao,
        ).normalized()
    return None


# ---------------------------------------------------------------------------
# JSONL ledger
# ---------------------------------------------------------------------------

_LEDGER_LOCK = threading.Lock()


def _ledger_path() -> Optional[Path]:
    p = (os.getenv("AGENT_LLM_LEDGER_PATH") or "").strip()
    return Path(p) if p else None


def _ledger_url() -> Optional[str]:
    u = (os.getenv("AGENT_LLM_LEDGER_URL") or "").strip()
    return u or None


def _append_ledger_http(row: Dict[str, Any], *, url: str, timeout_s: float = 2.0) -> None:
    data = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - internal URL
        # Drain the response to keep urllib happy; ignore body.
        _ = resp.read()


def append_ledger(row: Dict[str, Any], *, path: Optional[Path] = None, url: Optional[str] = None) -> None:
    """
    Append a single ledger row.

    When ``AGENT_LLM_LEDGER_URL`` is set, posts the row to that endpoint.
    Otherwise, when ``AGENT_LLM_LEDGER_PATH`` is set, appends JSONL locally.
    """
    try:
        u = url or _ledger_url()
        if u:
            _append_ledger_http(row, url=u)
            return
        p = path or _ledger_path()
        if p is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        with _LEDGER_LOCK:
            with p.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as exc:
        _logger.warning("llm_usage: failed to append ledger row: %s", exc)


def _extract_request_id(resp: Any) -> Optional[str]:
    """OpenAI SDK >=1.x exposes ``_request_id`` on responses."""
    rid = getattr(resp, "_request_id", None) or getattr(resp, "id", None)
    if isinstance(rid, str) and rid:
        return rid
    return None


# ---------------------------------------------------------------------------
# Aggregating recorder
# ---------------------------------------------------------------------------

@dataclass
class _Slot:
    usage: TokenUsage = field(default_factory=TokenUsage)
    calls: int = 0


class LLMUsageRecorder:
    """
    Aggregates token usage + estimated cost, bucketed by (provider, model, tier).

    Also writes a per-call JSONL ledger row when :func:`record_call` or
    :func:`add` is invoked with ``ledger_meta`` supplied, or unconditionally
    when ``AGENT_LLM_LEDGER_PATH`` is set.
    """

    def __init__(self, *, ledger_path: Optional[str | Path] = None) -> None:
        self._by_key: Dict[Tuple[str, str, str], _Slot] = {}
        self._ledger_path: Optional[Path] = Path(ledger_path) if ledger_path else _ledger_path()
        self._ledger_url: Optional[str] = _ledger_url()
        self._lock = threading.Lock()

    # --- aggregation ------------------------------------------------------

    def add(
        self,
        *,
        provider: str,
        model: str,
        usage: TokenUsage,
        tier: Tier = TIER_STANDARD,
        ledger_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Add a single call's usage to the in-memory aggregate, and optionally
        append a ledger row.

        ``ledger_meta`` may include things like ``request_id``, ``endpoint``,
        ``trace_id``, ``user_id``, etc. Pass ``None`` to skip ledger writes
        for this call even if the env var is set.
        """
        provider = (provider or "unknown").strip().lower()
        model = (model or "unknown").strip()

        # Auto-detect fine-tune tier from the model id if the caller did not
        # specify one explicitly.
        if tier == TIER_STANDARD and model.startswith("ft:"):
            tier = TIER_FINETUNE

        key = (provider, model, tier)
        with self._lock:
            slot = self._by_key.get(key)
            if slot is None:
                slot = _Slot()
                self._by_key[key] = slot
            slot.usage.add(usage.normalized())
            slot.calls += 1

        if ledger_meta is not None and (self._ledger_url is not None or self._ledger_path is not None):
            cost = estimate_cost_usd(model=model, usage=usage, tier=tier)
            row: Dict[str, Any] = {
                "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "provider": provider,
                "model": model,
                "tier": tier,
                "usage": usage.normalized().to_dict(),
                "cost_usd": None if cost is None else round(float(cost), 8),
            }
            row.update({k: v for k, v in ledger_meta.items() if k not in row})
            append_ledger(row, path=self._ledger_path, url=self._ledger_url)

    def record_call(
        self,
        *,
        provider: str,
        model: str,
        response: Any,
        tier: Tier = TIER_STANDARD,
        endpoint: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[TokenUsage]:
        """
        Convenience: extract usage from a raw response object, record it, and
        also append a ledger row (when ``AGENT_LLM_LEDGER_PATH`` is set) with
        the ``request_id`` and any extra metadata.
        """
        u = extract_token_usage(response)
        if u is None:
            return None
        meta: Dict[str, Any] = {
            "request_id": _extract_request_id(response),
            "endpoint": endpoint,
        }
        if extra:
            meta.update(extra)
        self.add(provider=provider, model=model, usage=u, tier=tier, ledger_meta=meta)
        return u

    # --- reporting --------------------------------------------------------

    def snapshot(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self._lock:
            items = sorted(self._by_key.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2]))
        for (provider, model, tier), slot in items:
            u = slot.usage.normalized()
            cost = estimate_cost_usd(model=model, usage=u, tier=tier)
            out.append(
                {
                    "provider": provider,
                    "model": model,
                    "tier": tier,
                    "calls": slot.calls,
                    "input_tokens": u.input_tokens,
                    "output_tokens": u.output_tokens,
                    "total_tokens": u.total_tokens,
                    "cached_input_tokens": u.cached_input_tokens,
                    "reasoning_tokens": u.reasoning_tokens,
                    "audio_input_tokens": u.audio_input_tokens,
                    "audio_output_tokens": u.audio_output_tokens,
                    "estimated_cost_usd": None if cost is None else round(float(cost), 6),
                }
            )
        return out

    def totals(self) -> dict[str, Any]:
        tot = TokenUsage()
        cost_sum = 0.0
        has_cost = False
        with self._lock:
            items = list(self._by_key.items())
        for (_provider, model, tier), slot in items:
            u = slot.usage.normalized()
            tot.add(u)
            c = estimate_cost_usd(model=model, usage=u, tier=tier)
            if c is not None:
                has_cost = True
                cost_sum += float(c)
        t = tot.normalized()
        return {
            "models": len({(p, m) for (p, m, _t) in [k for k, _ in items]}),
            "calls": sum(slot.calls for _k, slot in items),
            "input_tokens": t.input_tokens,
            "output_tokens": t.output_tokens,
            "total_tokens": t.total_tokens,
            "cached_input_tokens": t.cached_input_tokens,
            "reasoning_tokens": t.reasoning_tokens,
            "audio_input_tokens": t.audio_input_tokens,
            "audio_output_tokens": t.audio_output_tokens,
            "estimated_cost_usd": None if not has_cost else round(float(cost_sum), 6),
        }


# ---------------------------------------------------------------------------
# Wrappers
# ---------------------------------------------------------------------------

class TokenTrackingLLM:
    """
    Thin wrapper that proxies calls to an underlying LangChain-style LLM,
    records token usage when the response exposes it, and — when an
    :class:`~agent.agent_control.AgentControl` is available — blocks the
    call while the orchestrator has asked the agent to pause and raises
    :class:`~agent.agent_control.TakeoverRequested` when a takeover has
    been requested.
    """

    def __init__(
        self,
        llm: Any,
        *,
        provider: str,
        model: str,
        recorder: LLMUsageRecorder,
        tier: Tier = TIER_STANDARD,
    ) -> None:
        self._llm = llm
        self._provider = provider
        self._model = model
        self._recorder = recorder
        self._tier = tier

    def _apply_operator_guidance(self, args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
        ctl = self._control()
        if ctl is None or not getattr(ctl, "enabled", False):
            return args, kwargs
        txt = _operator_guidance_text(ctl)
        if not txt:
            return args, kwargs
        return _inject_operator_guidance_messages(args, kwargs, txt)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._llm, name)

    def _control(self) -> Any:
        # Imported lazily so llm_usage stays usable outside the agent runtime.
        try:
            from .agent_control import get_default  # type: ignore
        except Exception:
            return None
        return get_default()

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        ctl = self._control()
        if ctl is not None:
            await ctl.gate_async()
        args, kwargs = self._apply_operator_guidance(args, kwargs)
        res = await self._llm.ainvoke(*args, **kwargs)
        self._recorder.record_call(
            provider=self._provider,
            model=self._model,
            response=res,
            tier=self._tier,
            endpoint="langchain.ainvoke",
        )
        return res

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        ctl = self._control()
        if ctl is not None:
            ctl.gate()
        args, kwargs = self._apply_operator_guidance(args, kwargs)
        res = self._llm.invoke(*args, **kwargs)
        self._recorder.record_call(
            provider=self._provider,
            model=self._model,
            response=res,
            tier=self._tier,
            endpoint="langchain.invoke",
        )
        return res

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._llm, "__call__", None)
        if callable(fn):
            ctl = self._control()
            if ctl is not None:
                await ctl.gate_async()
            args, kwargs = self._apply_operator_guidance(args, kwargs)
            res = await fn(*args, **kwargs)
            self._recorder.record_call(
                provider=self._provider,
                model=self._model,
                response=res,
                tier=self._tier,
                endpoint="langchain.__call__",
            )
            return res
        raise TypeError("Underlying LLM is not callable")


def _ensure_stream_usage(kwargs: dict) -> dict:
    """If the caller requested streaming, make sure we still get a final usage chunk."""
    if kwargs.get("stream"):
        so = kwargs.get("stream_options") or {}
        if isinstance(so, dict) and not so.get("include_usage"):
            so = {**so, "include_usage": True}
            kwargs = {**kwargs, "stream_options": so}
    return kwargs


class _TrackedCreate:
    """Callable wrapper around ``client.<resource>.create`` that records usage."""

    def __init__(
        self,
        inner_create,
        *,
        recorder: LLMUsageRecorder,
        provider: str,
        endpoint: str,
        default_tier: Tier,
        auto_stream_usage: bool,
    ) -> None:
        self._create = inner_create
        self._recorder = recorder
        self._provider = provider
        self._endpoint = endpoint
        self._default_tier = default_tier
        self._auto_stream_usage = auto_stream_usage

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self._auto_stream_usage:
            kwargs = _ensure_stream_usage(kwargs)
        model = kwargs.get("model") or (args[0] if args else "") or "unknown"
        tier = self._default_tier
        if isinstance(model, str) and model.startswith("ft:"):
            tier = TIER_FINETUNE
        t0 = time.perf_counter()
        res = self._create(*args, **kwargs)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        # Streaming: returned object is an iterator; usage arrives on the last
        # chunk. Wrap it so aggregation happens when the caller consumes it.
        if kwargs.get("stream"):
            return _StreamingProxy(
                res,
                recorder=self._recorder,
                provider=self._provider,
                model=str(model),
                tier=tier,
                endpoint=self._endpoint,
                elapsed_ms=elapsed_ms,
            )

        self._recorder.record_call(
            provider=self._provider,
            model=str(model),
            response=res,
            tier=tier,
            endpoint=self._endpoint,
            extra={"elapsed_ms": elapsed_ms},
        )
        return res


class _StreamingProxy:
    """
    Wraps a streaming response so we still record token usage from the final
    chunk that carries ``usage`` (requires ``stream_options.include_usage``).
    """

    def __init__(
        self,
        stream: Any,
        *,
        recorder: LLMUsageRecorder,
        provider: str,
        model: str,
        tier: Tier,
        endpoint: str,
        elapsed_ms: int,
    ) -> None:
        self._stream = stream
        self._recorder = recorder
        self._provider = provider
        self._model = model
        self._tier = tier
        self._endpoint = endpoint
        self._elapsed_ms = elapsed_ms
        self._last_usage: Optional[TokenUsage] = None
        self._last_request_id: Optional[str] = None

    def __iter__(self) -> Iterable[Any]:
        try:
            for chunk in self._stream:
                self._last_request_id = self._last_request_id or _extract_request_id(chunk)
                u = extract_token_usage(chunk)
                if u is not None:
                    self._last_usage = u
                yield chunk
        finally:
            if self._last_usage is not None:
                self._recorder.add(
                    provider=self._provider,
                    model=self._model,
                    usage=self._last_usage,
                    tier=self._tier,
                    ledger_meta={
                        "request_id": self._last_request_id,
                        "endpoint": self._endpoint,
                        "elapsed_ms": self._elapsed_ms,
                        "streaming": True,
                    },
                )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


class TrackedOpenAI:
    """
    Wraps an ``openai.OpenAI`` client to auto-record token usage for
    ``chat.completions.create``, ``responses.create`` and
    ``embeddings.create``, including streamed calls.

    Usage::

        from openai import OpenAI
        from agent.llm_usage import LLMUsageRecorder, TrackedOpenAI

        recorder = LLMUsageRecorder()
        client = TrackedOpenAI(OpenAI(), recorder=recorder, provider="openai")

        # Then use `client` exactly like an openai.OpenAI instance.
    """

    def __init__(
        self,
        client: Any,
        *,
        recorder: LLMUsageRecorder,
        provider: str = "openai",
        default_tier: Tier = TIER_STANDARD,
        auto_stream_usage: bool = True,
    ) -> None:
        self._client = client
        self._recorder = recorder
        self._provider = provider
        self._default_tier = default_tier
        self._auto_stream_usage = auto_stream_usage
        self._patch_in_place()

    def _wrap(self, inner_create, endpoint: str) -> _TrackedCreate:
        return _TrackedCreate(
            inner_create,
            recorder=self._recorder,
            provider=self._provider,
            endpoint=endpoint,
            default_tier=self._default_tier,
            auto_stream_usage=self._auto_stream_usage,
        )

    def _patch_in_place(self) -> None:
        """
        Monkey-patch ``create`` on the resources so the caller can keep using
        the familiar ``client.chat.completions.create(...)`` spelling.
        """
        try:
            chat = getattr(self._client, "chat", None)
            completions = getattr(chat, "completions", None) if chat is not None else None
            if completions is not None and hasattr(completions, "create"):
                completions.create = self._wrap(completions.create, "openai.chat.completions.create")  # type: ignore[assignment]
        except Exception as exc:
            _logger.debug("llm_usage: failed to wrap chat.completions.create: %s", exc)

        try:
            responses = getattr(self._client, "responses", None)
            if responses is not None and hasattr(responses, "create"):
                responses.create = self._wrap(responses.create, "openai.responses.create")  # type: ignore[assignment]
        except Exception as exc:
            _logger.debug("llm_usage: failed to wrap responses.create: %s", exc)

        try:
            embeddings = getattr(self._client, "embeddings", None)
            if embeddings is not None and hasattr(embeddings, "create"):
                embeddings.create = self._wrap(embeddings.create, "openai.embeddings.create")  # type: ignore[assignment]
        except Exception as exc:
            _logger.debug("llm_usage: failed to wrap embeddings.create: %s", exc)

    def __getattr__(self, name: str) -> Any:
        # All other methods pass through transparently.
        return getattr(self._client, name)
