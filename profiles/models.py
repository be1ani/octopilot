from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


SCHEMA_VERSION = "1.0"


class ProficiencyLevel(str, Enum):
    beginner = "beginner"
    intermediate = "intermediate"
    advanced = "advanced"
    expert = "expert"


class Skill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    level: Optional[ProficiencyLevel] = None
    keywords: List[str] = Field(default_factory=list)


class ExperienceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    company: Optional[str] = None
    location: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    is_current: Optional[bool] = None
    highlights: List[str] = Field(default_factory=list)
    technologies: List[str] = Field(default_factory=list)


class EducationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    institution: str = Field(min_length=1)
    degree: Optional[str] = None
    field_of_study: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    grade: Optional[str] = None
    highlights: List[str] = Field(default_factory=list)


class Link(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    url: str = Field(min_length=3)


class LanguageItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    level: Optional[ProficiencyLevel] = None


class CertificationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    issuer: Optional[str] = None
    issued_date: Optional[date] = None
    expires_date: Optional[date] = None
    credential_id: Optional[str] = None
    url: Optional[str] = None


class ProjectItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    role: Optional[str] = None
    description: Optional[str] = None
    highlights: List[str] = Field(default_factory=list)
    technologies: List[str] = Field(default_factory=list)
    links: List[Link] = Field(default_factory=list)


class Preferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    desired_titles: List[str] = Field(default_factory=list)
    desired_industries: List[str] = Field(default_factory=list)
    desired_locations: List[str] = Field(default_factory=list)
    remote_preference: Optional[Literal["onsite", "hybrid", "remote"]] = None
    relocation_ok: Optional[bool] = None
    work_authorization: Optional[str] = None


class ResidentialAddress(BaseModel):
    """Structured home address (street, postal code, country)."""

    model_config = ConfigDict(extra="forbid")

    street: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None


class BaseInfo(BaseModel):
    """
    Base information shared across all profiles for an applicant.
    Mandatory fields here are intentionally minimal; most resumes don't contain everything reliably.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=1)
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    residential_address: Optional[ResidentialAddress] = None
    nationality: Optional[str] = None
    gender: Optional[str] = None
    birthdate: Optional[date] = None
    age: Optional[int] = Field(default=None, ge=0, le=120)
    years_of_experience: Optional[float] = Field(
        default=None,
        ge=0,
        description="Total professional experience in years (not stored as form-relative option codes).",
    )
    highest_degree: Optional[str] = Field(
        default=None,
        description="Current highest completed degree (e.g. M.Sc. Computer Science).",
    )
    headline: Optional[str] = None
    summary: Optional[str] = None

    skillset: List[Skill] = Field(default_factory=list)
    experience: List[ExperienceItem] = Field(default_factory=list)
    education: List[EducationItem] = Field(default_factory=list)


class OtherInfo(BaseModel):
    """
    Flexible-but-consistent bucket for everything else.

    Consistency rule: the keys/types here do not change across profile objects.
    Flexibility rule: use `custom` for additional fields you want to persist without changing the schema.
    """

    model_config = ConfigDict(extra="forbid")

    links: List[Link] = Field(default_factory=list)
    languages: List[LanguageItem] = Field(default_factory=list)
    certifications: List[CertificationItem] = Field(default_factory=list)
    projects: List[ProjectItem] = Field(default_factory=list)
    publications: List[str] = Field(default_factory=list)
    volunteer: List[str] = Field(default_factory=list)
    preferences: Preferences = Field(default_factory=Preferences)
    additional_notes: Optional[str] = None

    # Single controlled escape hatch (still consistent across all profiles).
    custom: Dict[str, Any] = Field(default_factory=dict)


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    profile_id: str = Field(min_length=1)
    profile_type: str = Field(min_length=1, description='e.g. "web_designer", "web_developer"')
    label: Optional[str] = Field(default=None, description='Human-friendly name, e.g. "Web Developer (React)"')

    base: BaseInfo
    other: OtherInfo = Field(default_factory=OtherInfo)

    source_pdf_path: Optional[str] = Field(
        default=None,
        description="Original PDF path the profile was extracted from. Audit-only; not used for runtime file resolution.",
    )
    attachments: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Documents uploaded by the user for this profile, stored under "
            "`attachments/<profile_id>/`. Map of display-name -> repo-relative path. "
            "The agent exposes every entry as an available file at runtime."
        ),
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

