from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

from profiles.models import BaseInfo, OtherInfo, Profile
from profiles.pdf_extract import extract_text_from_pdf


def profile_to_import_json(profile: Profile) -> dict[str, Any]:
    """
    Return a plain JSON-serializable dict representing a single Profile object.
    This is the exact shape expected by the orchestrator UI "Import JSON" flow.
    """
    return profile.model_dump(mode="json")


def build_profile_json_from_pdf(
    *,
    pdf_path: str,
    profile_id: str,
    profile_type: str,
    label: Optional[str] = None,
    model: Optional[str] = None,
    no_llm: bool = False,
    full_name: str = "Unknown",
) -> dict[str, Any]:
    """
    Convert a resume PDF into a Profile JSON dict.

    - If no_llm is True, produces a minimal profile shell.
    - Otherwise uses the LLM extractor (requires OPENAI_API_KEY).
    """
    text = extract_text_from_pdf(pdf_path)
    now = datetime.now(UTC)

    if no_llm:
        profile = Profile(
            profile_id=profile_id,
            profile_type=profile_type,
            label=label,
            base=BaseInfo(full_name=full_name),
            other=OtherInfo(),
            source_pdf_path=str(Path(pdf_path)),
            created_at=now,
            updated_at=now,
        )
    else:
        from profiles.llm_profile import build_profile_from_text_with_llm

        profile = build_profile_from_text_with_llm(
            resume_text=text,
            profile_id=profile_id,
            profile_type=profile_type,
            label=label,
            source_pdf_path=str(Path(pdf_path)),
            model=model,
        )
        profile.source_pdf_path = str(Path(pdf_path))
        profile.updated_at = now
        if not profile.created_at:
            profile.created_at = now

    return profile_to_import_json(profile)

