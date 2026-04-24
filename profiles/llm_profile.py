from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any, Dict, Optional

from .models import Profile, SCHEMA_VERSION
from agent.llm_usage import (
    LLMUsageRecorder,
    TrackedOpenAI,
    estimate_cost_usd,
    extract_token_usage,
)


def _profile_json_schema() -> Dict[str, Any]:
    # Pydantic v2 JSON schema. We keep the schema stable by pinning `SCHEMA_VERSION`.
    return Profile.model_json_schema()


def build_profile_from_text_with_llm(
    *,
    resume_text: str,
    profile_id: str,
    profile_type: str,
    label: Optional[str] = None,
    source_pdf_path: Optional[str] = None,
    model: Optional[str] = None,
) -> Profile:
    try:
        from openai import OpenAI  # type: ignore
    except ModuleNotFoundError as e:
        raise RuntimeError('Missing dependency "openai". Install it with: pip install -r requirements.txt') from e

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")

    # Wrap the OpenAI client so every call transparently records token usage
    # (including cached-input / reasoning / audio sub-fields) and appends a
    # ledger row when AGENT_LLM_LEDGER_PATH is set.
    recorder = LLMUsageRecorder()
    client = TrackedOpenAI(OpenAI(api_key=api_key), recorder=recorder, provider="openai")
    model = model or os.environ.get("PROFILE_LLM_MODEL", "gpt-4.1-mini")

    schema = _profile_json_schema()

    system = (
        "You extract structured resume data.\n"
        "Return ONLY JSON that matches the provided JSON Schema. No prose, no markdown.\n"
        "If a field is unknown, use null or an empty list/object as appropriate.\n"
        "Dates must be ISO-8601 (YYYY-MM-DD)."
    )

    user = {
        "task": "Convert resume text into a Profile object.",
        "schema_version": SCHEMA_VERSION,
        "profile_id": profile_id,
        "profile_type": profile_type,
        "label": label,
        "source_pdf_path": source_pdf_path,
        "resume_text": resume_text[:120_000],
    }

    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(
                {
                    **user,
                    "json_schema": schema,
                    "instructions": (
                        "Return a single JSON object ONLY. "
                        "It MUST validate against json_schema. "
                        "Do not include backticks or any other wrapper."
                    ),
                },
                ensure_ascii=False,
            ),
        },
    ]

    usage_obj = None
    # Prefer `responses.create` with strict JSON Schema when available.
    try:
        resp = client.responses.create(
            model=model,
            input=messages,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "Profile",
                    "schema": schema,
                    "strict": True,
                },
            },
        )
        usage_obj = resp
        content = resp.output_text
    except TypeError:
        # Compatibility fallback for SDKs where `responses.create(..., response_format=...)` isn't supported.
        chat = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={"type": "json_object"},
        )
        usage_obj = getattr(chat, "usage", None) or chat
        content = (chat.choices[0].message.content or "").strip()

    obj = json.loads(content)

    # Some models occasionally emit nulls for created_at/updated_at even though the schema
    # expects datetimes. Treat null as "missing" and let defaults (or our stamps) apply.
    if isinstance(obj, dict):
        for k in ("created_at", "updated_at"):
            if obj.get(k) is None:
                obj.pop(k, None)

    # Best-effort: print token usage + estimated cost (set PROFILE_LLM_PRINT_COST=1).
    try:
        if (os.getenv("PROFILE_LLM_PRINT_COST") or "").strip().lower() in ("1", "true", "yes"):
            u = extract_token_usage(usage_obj)
            if u:
                cost = estimate_cost_usd(model=model, usage=u)
                if cost is None:
                    print(f"[profile-llm] model={model} tokens={u.total_tokens} (pricing unknown)")
                else:
                    print(f"[profile-llm] model={model} tokens={u.total_tokens} cost≈${cost:.4f}")
    except Exception:
        pass

    profile = Profile.model_validate(obj)
    now = datetime.now(UTC)
    profile.updated_at = now
    # Ensure a stable created_at even if missing.
    if not getattr(profile, "created_at", None):
        profile.created_at = now
    return profile

