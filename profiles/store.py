from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .models import Profile
import os
import urllib.parse
import urllib.request


def _orch_api_base() -> str:
    return (os.environ.get("ORCH_API_BASE") or "").strip().rstrip("/")


def _http_json(method: str, url: str, body: Any | None = None, timeout_s: float = 4.0) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # nosec - internal URL only
        raw = resp.read()
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))


DEFAULT_DB_PATH = Path("profiles_db.json")


def coerce_base_full_name_to_string(applicant_obj: dict[str, Any]) -> None:
    """
    Forms sometimes map first/last name separately; mistaken deep-sets can store
    base.full_name as {"first_name": ..., "last_name": ...}. Coerce to a single string.
    """
    profiles = applicant_obj.get("profiles")
    if not isinstance(profiles, dict):
        return
    for prof in profiles.values():
        if not isinstance(prof, dict):
            continue
        base = prof.get("base")
        if not isinstance(base, dict):
            continue
        fn = base.get("full_name")
        if isinstance(fn, dict):
            first = str(fn.get("first_name") or "").strip()
            last = str(fn.get("last_name") or "").strip()
            merged = f"{first} {last}".strip() or "Unknown"
            base["full_name"] = merged


def normalize_base_address(applicant_obj: dict[str, Any]) -> None:
    """
    - Legacy: deep-set paths like base.address.city may leave address as a dict before validation.
    - Prefer structured `residential_address` (street, postal_code, country) in profiles_db.json;
      also keep a single-line `address` string for tools that merge form fields into base.address.
    """
    profiles = applicant_obj.get("profiles")
    if not isinstance(profiles, dict):
        return
    for prof in profiles.values():
        if not isinstance(prof, dict):
            continue
        base = prof.get("base")
        if not isinstance(base, dict):
            continue
        addr = base.get("address")
        ra = base.get("residential_address")

        if isinstance(addr, dict):
            street = str(addr.get("street") or "").strip()
            z = str(addr.get("zip_code") or addr.get("postal_code") or "").strip()
            c = str(addr.get("city") or "").strip()
            co = str(addr.get("country") or "").strip()
            merged_ra = {k: v for k, v in {"street": street, "postal_code": z, "country": co}.items() if v}
            if merged_ra:
                base["residential_address"] = merged_ra
            mid = f"{z} {c}".strip() if (z or c) else ""
            bits = [b for b in (street, mid, co) if b]
            base["address"] = ", ".join(bits) if bits else None
        elif isinstance(ra, dict) and not base.get("address"):
            street = str(ra.get("street") or "").strip()
            z = str(ra.get("postal_code") or ra.get("zip_code") or "").strip()
            co = str(ra.get("country") or "").strip()
            mid = z
            bits = [b for b in (street, mid, co) if b]
            if bits:
                base["address"] = ", ".join(bits)


def strip_legacy_top_level_fields(applicant_obj: dict[str, Any]) -> None:
    """
    Drop fields that older profile JSON may carry but which the current Profile
    model no longer accepts (extra="forbid"). Keeps loads from blowing up on
    legacy data without doing any data migration.

    Drops:
      - top-level `resume_path` (or `base.resume_path`): replaced by profile.attachments
      - `other.custom.documents`: replaced by profile.attachments
    """
    legacy_top_level = ("resume_path",)
    profiles = applicant_obj.get("profiles")
    if not isinstance(profiles, dict):
        return
    for prof in profiles.values():
        if not isinstance(prof, dict):
            continue
        base = prof.get("base")
        if isinstance(base, dict):
            for k in legacy_top_level:
                base.pop(k, None)
        for k in legacy_top_level:
            prof.pop(k, None)
        other = prof.get("other")
        if isinstance(other, dict):
            custom = other.get("custom")
            if isinstance(custom, dict):
                custom.pop("documents", None)


def unwrap_described_fields(obj: Any) -> Any:
    """
    profiles_db.json may store each leaf as {"value": ..., "description": "..."}.
    Normalize to plain JSON before Pydantic validation.
    """
    if isinstance(obj, dict):
        if set(obj.keys()) == {"value", "description"}:
            return unwrap_described_fields(obj["value"])
        return {k: unwrap_described_fields(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [unwrap_described_fields(x) for x in obj]
    return obj


class ProfileStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)

    def load(self) -> Dict[str, Profile]:
        if not self.db_path.exists():
            return {}
        raw = self.db_path.read_text(encoding="utf-8") or ""
        raw = raw.lstrip("\ufeff").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            hint = ""
            first = raw.lstrip()[:1]
            if first and first not in "{[":
                hint = (
                    " File does not look like JSON (e.g. logs or shell output may have been saved here by mistake)."
                    " Restore profiles_db.json from version control or replace it with valid JSON."
                )
            raise ValueError(f"Invalid JSON in {self.db_path}: {e}.{hint}") from e
        if not isinstance(data, dict):
            raise ValueError("profiles_db.json root must be an object")
        data = unwrap_described_fields(data)
        # Back-compat: older DB shape nested profiles under a top-level grouping key.
        # New shape is {profile_id: profileObj}.
        out: Dict[str, Profile] = {}

        # Detect old shape by presence of any top-level object containing "profiles".
        old_like = False
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("profiles"), dict):
                old_like = True
                break

        if old_like:
            for _applicant_id, obj in data.items():
                if not isinstance(obj, dict):
                    continue
                profiles = obj.get("profiles")
                if not isinstance(profiles, dict):
                    continue
                for pid, prof_obj in profiles.items():
                    if not isinstance(pid, str) or not isinstance(prof_obj, dict):
                        continue
                    # Normalize legacy edits inside the profile blob.
                    coerce_base_full_name_to_string({"profiles": {pid: prof_obj}})
                    normalize_base_address({"profiles": {pid: prof_obj}})
                    strip_legacy_top_level_fields({"profiles": {pid: prof_obj}})
                    out[pid] = Profile.model_validate(prof_obj)
            return out

        for pid, prof_obj in data.items():
            if not isinstance(pid, str) or not isinstance(prof_obj, dict):
                continue
            coerce_base_full_name_to_string({"profiles": {pid: prof_obj}})
            normalize_base_address({"profiles": {pid: prof_obj}})
            strip_legacy_top_level_fields({"profiles": {pid: prof_obj}})
            out[pid] = Profile.model_validate(prof_obj)
        return out

    def save(self, profiles: Dict[str, Profile]) -> None:
        payload = {k: v.model_dump(mode="json") for k, v in profiles.items()}
        self.db_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def upsert_profile(self, profile: Profile) -> None:
        profiles = self.load()
        profiles[profile.profile_id] = profile
        self.save(profiles)


class OrchestratorProfileStore:
    """
    Profile store backed by the orchestrator HTTP API (which itself persists to Mongo).

    This is used inside agent containers so we don't rely on bind-mounted JSON files.
    """

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or _orch_api_base()).strip().rstrip("/")
        if not self.base_url:
            raise ValueError("ORCH_API_BASE is not set; cannot use OrchestratorProfileStore")

    def get_profile(self, profile_id: str) -> Profile:
        pid = urllib.parse.quote(str(profile_id))
        data = _http_json("GET", f"{self.base_url}/api/profiles/{pid}", None, timeout_s=6.0)
        prof = data.get("profile") if isinstance(data, dict) else None
        if not isinstance(prof, dict):
            raise KeyError(f"Profile not found: {profile_id}")
        # Be tolerant of legacy keys stored on older profile documents (the active
        # model declares extra="forbid", so any stale field would otherwise trip
        # validation).
        strip_legacy_top_level_fields({"profiles": {profile_id: prof}})
        return Profile.model_validate(prof)

    def upsert_profile(self, profile: Profile) -> None:
        pid = urllib.parse.quote(str(profile.profile_id))
        _http_json(
            "PUT",
            f"{self.base_url}/api/profiles/{pid}",
            {"profile": profile.model_dump(mode="json")},
            timeout_s=8.0,
        )

