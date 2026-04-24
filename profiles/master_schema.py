from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


SchemaCategory = Literal["absolute", "relative"]


class MasterField(BaseModel):
    """
    A single field definition in the master schema.

    - absolute: stable across jobs (name, email, phone, etc.)
    - relative: job-dependent (salary, relocation, notice period, etc.)
    - unrecognized: discovered dynamically from a job form; kept for review
    """

    key: str = Field(description="Stable key, e.g. 'base.full_name' or 'relative.salary_expectation'")
    label: str | None = None
    category: SchemaCategory = "relative"
    description: str | None = None
    unrecognized: bool = False


class MasterSchemaStore(BaseModel):
    schema_version: str = "1.0"
    fields: dict[str, MasterField] = Field(default_factory=dict)


DEFAULT_FIELDS: list[MasterField] = [
    # --- Absolute (stable) ---
    MasterField(
        key="base.full_name",
        label="Full name",
        category="absolute",
        description="Applicant's legal or preferred full name (as it should appear on the application).",
    ),
    MasterField(
        key="base.email",
        label="Email",
        category="absolute",
        description="Primary email address used for application communication.",
    ),
    MasterField(
        key="base.phone",
        label="Phone",
        category="absolute",
        description="Primary phone number (include country code if applicable).",
    ),
    MasterField(
        key="base.address",
        label="Address",
        category="absolute",
        description="Mailing address or location string the form expects (can be city/region/country if that's what the form asks for).",
    ),
    MasterField(
        key="base.age",
        label="Age",
        category="absolute",
        description="Applicant age in years. Only provide if explicitly requested and appropriate for the locale/legal context.",
    ),
    MasterField(
        key="other.preferences.work_authorization",
        label="Work authorization",
        category="absolute",
        description="Applicant's work authorization/right-to-work status for the job location (e.g. citizen, permanent resident, visa status).",
    ),
    # --- Relative (job-dependent) ---
    MasterField(
        key="relative.salary_expectation",
        label="Salary expectation",
        category="relative",
        description="Expected compensation for this specific role (number/range; specify gross vs net if relevant).",
    ),
    MasterField(
        key="relative.currency",
        label="Salary currency",
        category="relative",
        description="Currency for the salary expectation (e.g. EUR, USD, GBP).",
    ),
    MasterField(
        key="relative.start_date",
        label="Earliest start date",
        category="relative",
        description="Earliest date the applicant can start for this job (consider notice period and availability).",
    ),
    MasterField(
        key="relative.notice_period",
        label="Notice period",
        category="relative",
        description="Notice period required to leave current employment (e.g. '2 weeks', '1 month', 'immediate').",
    ),
    MasterField(
        key="relative.relocation_ok",
        label="Relocation",
        category="relative",
        description="Whether the applicant is willing to relocate for this specific job/location.",
    ),
    MasterField(
        key="relative.remote_preference",
        label="Remote preference",
        category="relative",
        description="Preferred working arrangement for this job: onsite, hybrid, or remote (if the form offers these choices).",
    ),
    MasterField(
        key="relative.visa_sponsorship_needed",
        label="Visa sponsorship needed",
        category="relative",
        description="Whether the applicant would require visa sponsorship for this job's location.",
    ),
]


def load_master_schema(path: str | Path = "master_profile_schema.json") -> MasterSchemaStore:
    p = Path(path)
    if p.exists() and p.is_file():
        try:
            obj = json.loads(p.read_text(encoding="utf-8") or "{}")
            # Backwards compatibility: older versions stored "values" here.
            if isinstance(obj, dict) and "values" in obj:
                obj = {k: v for k, v in obj.items() if k != "values"}
            store = MasterSchemaStore.model_validate(obj)
        except Exception:
            store = MasterSchemaStore()
    else:
        store = MasterSchemaStore()

    # Ensure defaults exist, and backfill missing labels/descriptions (do not overwrite user edits).
    for f in DEFAULT_FIELDS:
        upsert_field(
            store,
            key=f.key,
            label=f.label,
            category=f.category,
            description=f.description,
            unrecognized=f.unrecognized,
        )
    return store


def save_master_schema(store: MasterSchemaStore, path: str | Path = "master_profile_schema.json") -> None:
    p = Path(path)
    p.write_text(json.dumps(store.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def upsert_field(
    store: MasterSchemaStore,
    *,
    key: str,
    label: str | None = None,
    category: SchemaCategory = "relative",
    description: str | None = None,
    unrecognized: bool = False,
) -> MasterField:
    key = key.strip()
    if not key:
        raise ValueError("Field key must be non-empty")

    existing = store.fields.get(key)
    if existing:
        if label and not existing.label:
            existing.label = label
        if description and not existing.description:
            existing.description = description
        # Only flip to absolute explicitly; never implicitly downgrade absolute -> relative
        if category == "absolute" and existing.category != "absolute":
            existing.category = "absolute"
        # If we recognized it later, clear unrecognized flag
        if not unrecognized:
            existing.unrecognized = False
        return existing

    f = MasterField(
        key=key,
        label=label,
        category=category,
        description=description,
        unrecognized=unrecognized,
    )
    store.fields[key] = f
    return f


