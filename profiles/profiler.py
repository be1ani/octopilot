from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Literal

# Best-effort: enable readline so arrow keys and history work during input() and
# don't corrupt the value with raw ANSI escape bytes (e.g. "720\x1b[C00" for salary).
try:
    import readline  # noqa: F401  (import side effect only)
except Exception:
    pass


_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)")


def _sanitize_user_input(s: str) -> str:
    """Strip ANSI escape sequences and other C0 control chars from terminal input."""
    if not s:
        return s
    s = _ANSI_OSC_RE.sub("", s)
    s = _ANSI_CSI_RE.sub("", s)
    s = "".join(ch for ch in s if ch == "\t" or ch == "\n" or ord(ch) >= 0x20)
    return s

from pydantic import BaseModel, Field

from profiles.master_schema import MasterSchemaStore, load_master_schema, save_master_schema, upsert_field

HumanValueKind = Literal[
    "text",
    "number",
    "date",
    "multiline",
    "single_select",
    "multi_select",
    "boolean",
    "file_path",
]


class FieldUiSpec(BaseModel):
    display_name: str = Field(description="Short label for the UI, e.g. 'Birth date'.")
    value_kind: HumanValueKind = Field(
        default="multiline",
        description="Input control: text, number, date, multiline, single_select, multi_select, boolean, file_path.",
    )
    help_text: str | None = Field(
        default=None,
        description="Optional extra guidance (format, examples, form wording).",
    )
    options: list[dict[str, str]] | None = Field(
        default=None,
        description='For select kinds: [{"value":"internal","label":"Shown"}, ...].',
    )
    sensitive: bool = Field(default=False, description="If true, mask input in the orchestrator UI.")
    validation: dict[str, Any] | None = Field(
        default=None,
        description="Optional soft rules: pattern (regex), min, max, required (bool).",
    )


class DocumentUiSpec(BaseModel):
    display_name: str | None = Field(default=None, description="Label for document upload in the UI.")
    help_text: str | None = Field(default=None, description="Optional extra guidance for this upload slot.")


class AskUserMissingInfoParams(BaseModel):
    field_path: str
    question: str
    ui: FieldUiSpec | None = Field(
        default=None,
        description="Structured UI metadata; if omitted, a generic multiline prompt is used.",
    )

from profiles.models import Profile
from profiles.store import OrchestratorProfileStore, ProfileStore


class FieldRequest(BaseModel):
    key: str = Field(description="Stable key. If unknown, pass a sensible stable key; it will be tracked as unrecognized.")
    label: str | None = None
    prompt: str | None = None
    default: str | None = None
    ui: FieldUiSpec | None = Field(
        default=None,
        description="Structured UI metadata from the model (recommended for orchestrator runs).",
    )


class ResolveFieldsParams(BaseModel):
    fields: list[FieldRequest]


class DocumentRequest(BaseModel):
    key: str = Field(description="Stable key for this document, e.g. 'documents.resume' or 'documents.other'.")
    label: str | None = Field(default=None, description="Human-friendly label, e.g. 'Resume', 'Cover letter', 'Other'.")
    ui: DocumentUiSpec | None = Field(default=None, description="Optional UI hints for the orchestrator panel.")
    required: bool = Field(default=False, description="Whether the upload is required by the form.")
    explicitly_specified: bool = Field(
        default=False,
        description="True if the form explicitly specifies the document type; False if it's generic 'Other'.",
    )
    allow_multiple: bool = Field(
        default=False,
        description="If True, user may provide multiple file paths for this single document slot.",
    )
    min_files: int = Field(
        default=0,
        ge=0,
        description="Minimum number of files required when allow_multiple=True. If required=True, will be treated as at least 1.",
    )


class ResolveDocumentsParams(BaseModel):
    documents: list[DocumentRequest]


def _parse_full_name_string(full: str | None) -> tuple[str, str]:
    full = (full or "").strip()
    if not full:
        return "", ""
    parts = full.split(None, 1)
    first = parts[0]
    last = parts[1] if len(parts) > 1 else ""
    return first, last


# Virtual paths under base.address.* merge into the single BaseInfo.address string.
# Forms may use postal_code vs zip_code interchangeably; both map to the internal zip slot.
_ZIP_THEN_CITY_RE = re.compile(r"^(\d{4,6})\s+(.+)$")


def _canonical_address_subkey(subkey: str) -> str | None:
    if subkey == "postal_code":
        return "zip_code"
    if subkey in ("street", "zip_code", "city", "country"):
        return subkey
    return None


def _parse_address_string(addr: str | None) -> dict[str, str]:
    """Best-effort split of a one-line / comma-separated postal address."""
    out = {k: "" for k in ("street", "zip_code", "city", "country")}
    s = (addr or "").strip()
    if not s:
        return out
    parts = [p.strip() for p in s.split(",")]
    if len(parts) >= 3:
        out["street"] = parts[0]
        m = _ZIP_THEN_CITY_RE.match(parts[1])
        if m:
            out["zip_code"] = m.group(1)
            out["city"] = m.group(2).strip()
        else:
            out["city"] = parts[1]
        out["country"] = ", ".join(parts[2:]).strip()
    elif len(parts) == 2:
        out["street"] = parts[0]
        m = _ZIP_THEN_CITY_RE.match(parts[1])
        if m:
            out["zip_code"] = m.group(1)
            out["city"] = m.group(2).strip()
        else:
            out["city"] = parts[1]
    else:
        line = parts[0]
        m = re.search(r"^(.+?)\s+(\d{4,6})\s+([A-Za-zÀ-ÿ].+)$", line)
        if m:
            out["street"] = m.group(1).strip().rstrip(",")
            out["zip_code"] = m.group(2)
            out["city"] = m.group(3).strip()
    return out


def _address_parts_from_current(current: Any) -> dict[str, str]:
    if isinstance(current, str):
        return _parse_address_string(current)
    if isinstance(current, dict):
        z = str(current.get("zip_code") or current.get("postal_code") or "").strip()
        return {
            "street": str(current.get("street") or "").strip(),
            "zip_code": z,
            "city": str(current.get("city") or "").strip(),
            "country": str(current.get("country") or "").strip(),
        }
    return {k: "" for k in ("street", "zip_code", "city", "country")}


def _format_address_parts(p: dict[str, str]) -> str:
    street = (p.get("street") or "").strip()
    z = (p.get("zip_code") or "").strip()
    c = (p.get("city") or "").strip()
    co = (p.get("country") or "").strip()
    mid = f"{z} {c}".strip() if (z or c) else ""
    bits: list[str] = []
    if street:
        bits.append(street)
    if mid:
        bits.append(mid)
    if co:
        bits.append(co)
    return ", ".join(bits)


def _merge_address_field(current: Any, subkey: str, value: str) -> str:
    canon = _canonical_address_subkey(subkey)
    if canon is None:
        raise ValueError(f"unsupported address sub-key: {subkey}")
    parts = _address_parts_from_current(current)
    parts[canon] = value.strip()
    merged = _format_address_parts(parts)
    return merged if merged else "Unknown"


def _merge_full_name_parts(
    current_full: Any,
    *,
    first: str | None = None,
    last: str | None = None,
) -> str:
    """Build base.full_name string; first/last override parsed parts when not None."""
    cur_first, cur_last = "", ""
    if isinstance(current_full, str):
        cur_first, cur_last = _parse_full_name_string(current_full)
    elif isinstance(current_full, dict):
        cur_first = str(current_full.get("first_name") or "").strip()
        cur_last = str(current_full.get("last_name") or "").strip()
    new_first = cur_first if first is None else first.strip()
    new_last = cur_last if last is None else last.strip()
    return f"{new_first} {new_last}".strip()


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


# Forms / LLMs often use keys that differ from `Profile.base` (Pydantic `BaseInfo`) names.
_PROFILE_STORAGE_KEY_ALIASES: dict[str, str] = {
    "base.date_of_birth": "base.birthdate",
}


def _resolve_profile_storage_key(key: str) -> str:
    k = (key or "").strip()
    return _PROFILE_STORAGE_KEY_ALIASES.get(k, k)


class PromptUI:
    """
    Small prompt helper with:
    - highlighted prompts
    - optional browser field highlighter (set by the agent) so each interactive
      prompt scrolls to the relevant form field and flashes its container.
    """

    def __init__(self) -> None:
        # Callback signature: (label: str | None, key: str | None, prompt: str | None) -> None.
        # Runs synchronously, is best-effort, and must never raise.
        self.field_highlighter: Any = None

    def _highlight_field(
        self,
        *,
        label: str | None = None,
        key: str | None = None,
        prompt: str | None = None,
    ) -> None:
        fn = self.field_highlighter
        if not callable(fn):
            return
        try:
            fn(label, key, prompt)
        except Exception:
            return

    def _set_orch_attention(self, needed: bool, *, reason: str | None = None) -> None:
        """
        Best-effort notification to the orchestrator UI when we block on terminal input.
        This mirrors the behavior in `agent/cli.py` so prompts raised via `resolve_fields`
        also trigger the HUMAN badge/alarm.
        """
        base = (os.getenv("ORCH_API_BASE") or "").strip().rstrip("/")
        mid = (os.getenv("ORCH_MACHINE_ID") or "").strip()
        if not base or not mid:
            return
        try:
            payload: dict[str, Any] = {"needed": bool(needed)}
            if needed and reason:
                payload["reason"] = reason
            req = urllib.request.Request(
                f"{base}/api/machines/{mid}/attention",
                method="POST",
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            )
            with urllib.request.urlopen(req, timeout=2.0) as _resp:  # nosec - internal URL only
                _ = _resp.read()
        except Exception:
            return

    def _input_with_periodic_bell(self, prompt: str) -> str:
        # Notify orchestrator while we're blocked on input.
        reason = None
        try:
            p = (prompt or "").strip().replace("\n", " ")
            if p:
                reason = f"resolve input: {p[:160]}"
        except Exception:
            reason = "resolve input"

        self._set_orch_attention(True, reason=reason)
        try:
            return _sanitize_user_input(input(prompt))
        finally:
            self._set_orch_attention(False)

    def prompt_with_default(self, prompt: str, default: str | None) -> str:
        if default:
            suffix = f" [{default}]"
        else:
            # Always show a hint so the user knows the field is new/unknown
            # instead of staring at a blank prompt with no context.
            suffix = " [no previous value — type an answer, or press Enter to skip]"
        val = self._input_with_periodic_bell(f"\n\033[1;33m{prompt}{suffix}:\033[0m ").strip()
        return default if (not val and default is not None) else val

    def prompt_nonempty(self, prompt: str, default: str | None = None) -> str:
        while True:
            val = self.prompt_with_default(prompt, default)
            if val:
                return val
            print("Please enter a value.")

    def prompt_yes_no(self, prompt: str, default_no: bool = True) -> bool:
        default_hint = "y/N" if default_no else "Y/n"
        val = self._input_with_periodic_bell(f"\n\033[1;33m{prompt}\033[0m [{default_hint}]: ").strip().lower()
        if not val:
            return not default_no
        return val in {"y", "yes"}


class Profiler:
    """
    Owns:
    - master schema (structure + absolute/relative + unrecognized tracking)
    - all applicant-specific values stored in profiles_db.json

    The agent should just ask for info; Profiler decides defaults, prompts user,
    persists values, and keeps track of whether relative fields were used.
    """

    def __init__(
        self,
        *,
        db_path: str | Path,
        profile_id: str,
        schema_path: str | Path = "master_profile_schema.json",
        ui: PromptUI | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.schema_path = Path(schema_path)
        self.profile_id = profile_id
        self.ui = ui or PromptUI()

        # Prefer orchestrator-backed store when running inside orchestrated containers.
        # This makes profile reads/writes go to the Mongo-backed orchestrator API.
        use_orch = (os.environ.get("AGENT_PROFILE_STORE") or "").strip().lower() in ("orch", "orch_api", "orchestrator", "api")
        if not use_orch:
            use_orch = bool((os.environ.get("ORCH_API_BASE") or "").strip())
        if use_orch:
            self.store = OrchestratorProfileStore()
        else:
            self.store = ProfileStore(self.db_path)
        self.master: MasterSchemaStore = load_master_schema(self.schema_path)
        self.relative_used_in_current_form: bool = False
        # Keys already confirmed in this agent run; avoid re-prompting when the LLM calls resolve_fields again.
        self._session_resolved_field_keys: set[str] = set()

        self._profile = self._load_profile_model()

    def _load_profile_model(self) -> Profile:
        try:
            getp = getattr(self.store, "get_profile", None)
            if callable(getp):
                return getp(self.profile_id)
        except KeyError:
            raise SystemExit(f"Profile not found: {self.profile_id}")
        except Exception as e:
            raise SystemExit(f"Failed to load profile from orchestrator API: {e}") from e

        profiles = self.store.load()
        p = profiles.get(self.profile_id)
        if not p:
            raise SystemExit(f"Profile not found: {self.profile_id}")
        return p

    def _save_profile_model(self) -> None:
        self._profile.updated_at = self._profile.updated_at  # keep pydantic happy; store handles timestamps elsewhere
        self.store.upsert_profile(self._profile)

    def _profile_dict(self) -> dict[str, Any]:
        return self._profile.model_dump(mode="json")

    def _set_profile_from_dict(self, data: dict[str, Any]) -> None:
        self._profile = Profile.model_validate(data)

    def _custom_bucket(self) -> dict[str, Any]:
        d = self._profile_dict()
        other = d.setdefault("other", {})
        custom = other.setdefault("custom", {})
        if not isinstance(custom, dict):
            other["custom"] = {}
            custom = other["custom"]
        return custom

    def _get_value_from_profile_db(self, key: str) -> str | None:
        d = self._profile_dict()

        def _from_custom_maps() -> str | None:
            rk = _resolve_profile_storage_key(key)
            custom = self._custom_bucket()
            abs_map = custom.get("absolute_fields")
            rel_map = custom.get("relative_fields")
            if isinstance(abs_map, dict):
                for probe in (rk, key):
                    if probe in abs_map:
                        v = abs_map.get(probe)
                        if v not in (None, ""):
                            return str(v)
            if isinstance(rel_map, dict):
                for probe in (rk, key):
                    if probe in rel_map:
                        v = rel_map.get(probe)
                        if v not in (None, ""):
                            return str(v)
            return None

        if key.startswith("base."):
            mv = _from_custom_maps()
            if mv is not None:
                return mv
            if key in ("base.full_name.first_name", "base.full_name.last_name"):
                full = _deep_get(d, "base.full_name")
                if isinstance(full, dict):
                    sub = "first_name" if key.endswith("first_name") else "last_name"
                    v = full.get(sub)
                    return None if v in (None, "") else str(v)
                if isinstance(full, str):
                    first, last = _parse_full_name_string(full)
                    v = first if key.endswith("first_name") else last
                    return None if v in (None, "") else str(v)
                return None
            if key.startswith("base.address.") and key != "base.address":
                sub = key.removeprefix("base.address.")
                canon = _canonical_address_subkey(sub)
                if canon is not None:
                    raw = _deep_get(d, "base.address")
                    parts = _address_parts_from_current(raw)
                    v = parts.get(canon, "")
                    return None if v in (None, "") else str(v)
            rk = _resolve_profile_storage_key(key)
            for probe in (rk, key):
                v = _deep_get(d, probe)
                if v not in (None, ""):
                    return str(v)
            return None
        if key.startswith("other."):
            v = _deep_get(d, key)
            return None if v in (None, "") else str(v)

        custom = self._custom_bucket()
        if key.startswith("documents."):
            docs = custom.get("documents", {})
            if isinstance(docs, dict):
                v = docs.get(key)
                if v in (None, ""):
                    return None
                # For multi-file uploads we store a list; callers should not treat it as a single path.
                if isinstance(v, list):
                    # Return a human-friendly string (NOT JSON) to avoid users copying brackets/quotes.
                    return ", ".join([str(x) for x in v if isinstance(x, str)])
                return str(v)
            return None

        abs_map = custom.get("absolute_fields")
        if isinstance(abs_map, dict) and key in abs_map:
            v = abs_map.get(key)
            return None if v in (None, "") else str(v)
        rel_map = custom.get("relative_fields")
        if isinstance(rel_map, dict) and key in rel_map:
            v = rel_map.get(key)
            return None if v in (None, "") else str(v)

        return None

    def _get_raw_document_value(self, key: str) -> Any:
        d = self._profile_dict()
        try:
            docs = d.get("other", {}).get("custom", {}).get("documents", {})
            if isinstance(docs, dict):
                return docs.get(key)
        except Exception:
            pass
        return None

        abs_map = custom.get("absolute_fields", {})
        rel_map = custom.get("relative_fields", {})
        if isinstance(abs_map, dict) and key in abs_map:
            v = abs_map.get(key)
            return None if v in (None, "") else str(v)
        if isinstance(rel_map, dict) and key in rel_map:
            v = rel_map.get(key)
            return None if v in (None, "") else str(v)
        return None

    def _set_value_in_profile_db(self, key: str, value: Any, *, category: str) -> None:
        d = self._profile_dict()
        if key.startswith("base."):
            rk = _resolve_profile_storage_key(key)
            custom = d.setdefault("other", {}).setdefault("custom", {})
            if not isinstance(custom, dict):
                d["other"]["custom"] = {}
                custom = d["other"]["custom"]
            if category == "absolute":
                abs_map = custom.setdefault("absolute_fields", {})
                if not isinstance(abs_map, dict):
                    custom["absolute_fields"] = {}
                    abs_map = custom["absolute_fields"]
                abs_map[rk] = value
            else:
                rel_map = custom.setdefault("relative_fields", {})
                if not isinstance(rel_map, dict):
                    custom["relative_fields"] = {}
                    rel_map = custom["relative_fields"]
                rel_map[rk] = value
            self._set_profile_from_dict(d)
            self._save_profile_model()
            return
        if key.startswith("other."):
            _deep_set(d, key, value)
            self._set_profile_from_dict(d)
            self._save_profile_model()
            return

        custom = d.setdefault("other", {}).setdefault("custom", {})
        if not isinstance(custom, dict):
            d["other"]["custom"] = {}
            custom = d["other"]["custom"]

        if key.startswith("documents."):
            docs = custom.setdefault("documents", {})
            if not isinstance(docs, dict):
                custom["documents"] = {}
                docs = custom["documents"]
            docs[key] = value
        elif category == "absolute":
            abs_map = custom.setdefault("absolute_fields", {})
            if not isinstance(abs_map, dict):
                custom["absolute_fields"] = {}
                abs_map = custom["absolute_fields"]
            abs_map[key] = value
        else:
            rel_map = custom.setdefault("relative_fields", {})
            if not isinstance(rel_map, dict):
                custom["relative_fields"] = {}
                rel_map = custom["relative_fields"]
            rel_map[key] = value

        self._set_profile_from_dict(d)
        self._save_profile_model()

    def _ensure_field_in_schema(
        self,
        key: str,
        *,
        label: str | None,
        category: str,
        unrecognized: bool,
        description: str | None = None,
    ) -> None:
        # Upsert (not just insert) so we can backfill missing label/description over time
        # without overwriting existing user-edited values.
        upsert_field(
            self.master,
            key=key,
            label=label,
            category=category,
            description=(description.strip() if isinstance(description, str) and description.strip() else None),
            unrecognized=unrecognized,
        )
        save_master_schema(self.master, self.schema_path)

    @staticmethod
    def _orch_human_enabled() -> bool:
        from agent.orch_human_input import human_input_backend

        return human_input_backend() == "orch"

    def _orch_resolve_field_value(
        self,
        *,
        f: FieldRequest,
        mf: Any,
        label: str,
        prompt: str,
        default_str: str | None,
    ) -> tuple[str, bool]:
        from agent import orch_human_input as ohi

        spec = f.ui or FieldUiSpec(
            display_name=(label or f.key).strip() or f.key.strip(),
            value_kind="multiline",
            help_text=prompt,
        )
        show_promote = bool(mf.unrecognized)
        while True:
            rid = ohi.new_request_id()
            item = {
                "field_key": f.key.strip(),
                "display_name": spec.display_name,
                "help_text": (spec.help_text or prompt or "").strip() or None,
                "value_kind": spec.value_kind,
                "default_value": default_str,
                "options": spec.options,
                "sensitive": bool(spec.sensitive),
                "validation": spec.validation,
                "show_promote_to_absolute": show_promote,
            }
            resp = ohi.wait_human_response(
                request_id=rid,
                kind="field",
                item=item,
                attention_reason=f"resolve field: {spec.display_name}",
            )
            promote = bool(resp.get("promote_to_absolute"))
            raw = resp.get("value")
            if spec.value_kind == "multi_select":
                if isinstance(raw, list):
                    val_s = json.dumps([str(x) for x in raw], ensure_ascii=False)
                elif raw in (None, ""):
                    val_s = ""
                else:
                    val_s = str(raw).strip()
            elif spec.value_kind == "boolean":
                if isinstance(raw, bool):
                    val_s = "yes" if raw else "no"
                else:
                    val_s = str(raw).strip().lower()
                    if val_s in ("1", "true", "yes", "y"):
                        val_s = "yes"
                    elif val_s in ("0", "false", "no", "n"):
                        val_s = "no"
                    else:
                        val_s = ""
            else:
                val_s = "" if raw is None else str(raw).strip()

            if not val_s and default_str is not None:
                return str(default_str), promote
            if val_s:
                return val_s, promote

    def _orch_prompt_file_path(
        self,
        *,
        field_key: str,
        label: str,
        help_text: str,
        default_hint: str | None,
        show_promote: bool,
        attention_reason: str,
    ) -> tuple[str, bool]:
        from agent import orch_human_input as ohi

        rid = ohi.new_request_id()
        item = {
            "field_key": field_key,
            "display_name": label,
            "help_text": help_text,
            "value_kind": "file_path",
            "default_value": default_hint,
            "options": None,
            "sensitive": False,
            "validation": None,
            "show_promote_to_absolute": show_promote,
        }
        resp = ohi.wait_human_response(
            request_id=rid,
            kind="document",
            item=item,
            attention_reason=attention_reason,
        )
        promote = bool(resp.get("promote_to_absolute"))
        raw = resp.get("value")
        val_s = "" if raw is None else str(raw).strip()
        return val_s, promote

    def resolve_fields(self, params: ResolveFieldsParams) -> str:
        fields = params.fields
        pending: list[FieldRequest] = []
        for f in fields:
            key = f.key.strip()
            if not key:
                continue
            self._ensure_field_in_schema(
                key,
                label=f.label,
                category="relative",
                unrecognized=True,
                description=f.prompt or (f"Value for '{(f.label or key)}' as requested by a job application form."),
            )
            if key not in self._session_resolved_field_keys:
                pending.append(f)

        skipped_keys = [f.key.strip() for f in fields if f.key.strip() and f.key.strip() in self._session_resolved_field_keys]
        print("\n=== Fields required by this form ===")
        if skipped_keys:
            print(
                f"\033[2m({len(skipped_keys)} field(s) already resolved this session; skipping re-prompt.)\033[0m"
            )
            for k in skipped_keys:
                cached = self._get_value_from_profile_db(k)
                if cached not in (None, ""):
                    print(f"\033[2m  • {k} = {cached!r}\033[0m")
        if not pending:
            print(
                "\033[2mNo new fields to resolve — using values saved earlier this session.\033[0m"
            )
        for f in pending:
            key = f.key.strip()
            mf = self.master.fields.get(key)
            label = (mf.label if mf and mf.label else f.label) or key
            default_val = self._get_value_from_profile_db(key) or f.default
            cat = mf.category if mf else "relative"
            suffix = " (unrecognized)" if (mf.unrecognized if mf else True) else ""
            print(f"- [{cat}]{suffix} {label} ({key}): default={json.dumps(default_val, ensure_ascii=False)}")
        print("===================================\n")

        out: dict[str, str] = {}
        for f in pending:
            key = f.key.strip()
            if not key:
                continue
            # Ensure schema entry exists and has a description if possible.
            self._ensure_field_in_schema(
                key,
                label=f.label,
                category="relative",
                unrecognized=True,
                description=f.prompt or (f"Value for '{(f.label or key)}' as requested by a job application form."),
            )
            mf = self.master.fields.get(key)
            assert mf is not None

            label = (mf.label or f.label or key).strip()
            prompt = (f.prompt or f"Enter {label}").strip()

            default_val = self._get_value_from_profile_db(key) or f.default
            default_str = None if default_val in (None, "") else str(default_val)
            # Scroll to and highlight the corresponding field in the browser so
            # the user can see which control the question is about before
            # answering (especially useful when several similar fields exist).
            self.ui._highlight_field(label=label, key=key, prompt=prompt)
            orch_promote = False
            if self._orch_human_enabled():
                ans, orch_promote = self._orch_resolve_field_value(
                    f=f, mf=mf, label=label, prompt=prompt, default_str=default_str
                )
                ans = ans.strip() if isinstance(ans, str) else str(ans)
            else:
                ans = self.ui.prompt_with_default(prompt, default_str).strip()
                if not ans:
                    ans = self.ui.prompt_nonempty(prompt)

            out[key] = ans
            self._set_value_in_profile_db(key, ans, category=mf.category)
            self._session_resolved_field_keys.add(key)

            if mf.category == "relative":
                self.relative_used_in_current_form = True

            if mf.unrecognized:
                do_promote = orch_promote if self._orch_human_enabled() else self.ui.prompt_yes_no(
                    f'Use "{ans}" for all future prompts for "{label}" (promote to absolute)?',
                    default_no=True,
                )
                if do_promote:
                    mf.category = "absolute"
                    mf.unrecognized = False
                    upsert_field(self.master, key=key, label=label, category="absolute", unrecognized=False)
                    save_master_schema(self.master, self.schema_path)
                    # Move the stored value to absolute bucket
                    self._set_value_in_profile_db(key, ans, category="absolute")

        for f in fields:
            key = f.key.strip()
            if not key:
                continue
            if key in out:
                continue
            stored = self._get_value_from_profile_db(key)
            fallback = f.default
            val = stored if stored not in (None, "") else fallback
            if val not in (None, ""):
                out[key] = str(val)

        return json.dumps(out, ensure_ascii=False)

    def resolve_documents(self, params: ResolveDocumentsParams, *, available_files: list[str]) -> str:
        docs = params.documents
        if not docs:
            return "{}"

        print("\n=== Document uploads requested by this form ===")
        for d in docs:
            key = d.key.strip()
            if not key:
                continue
            self._ensure_field_in_schema(
                key,
                label=d.label,
                category="relative",
                unrecognized=not d.explicitly_specified,
                description=(
                    f"Document upload slot for '{(d.label or key)}' requested by a job application form."
                    if (d.label or key)
                    else None
                ),
            )
            mf = self.master.fields.get(key)
            label = (mf.label if mf and mf.label else d.label) or key
            default_val = self._get_raw_document_value(key)
            req = "required" if d.required else "optional"
            suffix = " (unrecognized)" if (mf.unrecognized if mf else False) else ""
            multi = " multi" if d.allow_multiple else ""
            print(f"- [{req}{multi}]{suffix} {label} ({key}): default={json.dumps(default_val, ensure_ascii=False)}")
        print("=============================================\n")

        out: dict[str, Any] = {}
        for d in docs:
            key = d.key.strip()
            if not key:
                continue

            self._ensure_field_in_schema(
                key,
                label=d.label,
                category="relative",
                unrecognized=not d.explicitly_specified,
                description=(
                    f"Document upload slot for '{(d.label or key)}' requested by a job application form."
                    if (d.label or key)
                    else None
                ),
            )
            mf = self.master.fields.get(key)
            assert mf is not None

            label = (mf.label or d.label or key).strip()
            if d.ui and d.ui.display_name:
                label = str(d.ui.display_name).strip() or label
            default_val = self._get_raw_document_value(key)

            if not d.required and not default_val:
                continue

            doc_help = (d.ui.help_text if d.ui and d.ui.help_text else None) or (
                f"Absolute file path on the agent machine for “{label}”."
            )
            orch_doc = self._orch_human_enabled()

            if d.allow_multiple:
                existing_list: list[str] = []
                if isinstance(default_val, list) and all(isinstance(x, str) for x in default_val):
                    existing_list = default_val
                elif isinstance(default_val, str) and default_val.strip():
                    existing_list = [default_val]

                print(f'\nProvide file paths for "{label}".')
                if not orch_doc:
                    print("- Enter one path per prompt.")
                    print("- Press Enter on an empty prompt to finish.\n")
                chosen_list: list[str] = []
                default_hint = existing_list[0] if len(existing_list) == 1 else None
                if existing_list and len(existing_list) > 1:
                    print(f"Default files:\n- " + "\n- ".join(existing_list) + "\n")

                if orch_doc:
                    any_promote = False
                    slot = 1
                    while True:
                        slot_label = f"{label} (file {slot})" if slot > 1 else label
                        hint = existing_list[slot - 1] if 0 <= slot - 1 < len(existing_list) else None
                        ht = doc_help if slot == 1 else f"{doc_help} Leave empty when finished adding files."
                        path_s, prom = self._orch_prompt_file_path(
                            field_key=key,
                            label=slot_label,
                            help_text=ht,
                            default_hint=hint,
                            show_promote=bool(mf.unrecognized),
                            attention_reason=f"document: {label}",
                        )
                        any_promote = any_promote or prom
                        if not path_s.strip():
                            break
                        chosen_list.append(path_s.strip())
                        slot += 1
                    orch_doc_promote = any_promote
                else:
                    orch_doc_promote = False
                    first = self.ui.prompt_with_default(f'File path for "{label}"', default_hint).strip()
                    if first:
                        if first.lstrip().startswith("["):
                            try:
                                parsed = json.loads(first)
                                if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
                                    chosen_list.extend(parsed)
                                else:
                                    chosen_list.append(first)
                            except Exception:
                                chosen_list.append(first)
                        else:
                            chosen_list.append(first)
                    while True:
                        nxt = self.ui.prompt_with_default(f'Another file for "{label}" (blank to finish)', None).strip()
                        if not nxt:
                            break
                        chosen_list.append(nxt)

                chosen_paths: list[str] = []
                for raw in chosen_list:
                    pth = str(Path(raw).expanduser())
                    if Path(pth).is_file():
                        chosen_paths.append(pth)
                    else:
                        if d.required:
                            raise InterruptedError(f"Required document path is not a file: {pth}")

                min_needed = max(1 if d.required else 0, int(d.min_files or 0))
                if len(chosen_paths) < min_needed:
                    raise InterruptedError(f'"{label}" requires at least {min_needed} file(s).')

                if not chosen_paths:
                    continue

                out[key] = chosen_paths
                self._set_value_in_profile_db(key, chosen_paths, category=mf.category)
                for pth in chosen_paths:
                    if pth not in available_files:
                        available_files.append(pth)
            else:
                single_default: str | None = None
                if isinstance(default_val, list) and default_val and isinstance(default_val[0], str):
                    single_default = default_val[0]
                elif isinstance(default_val, str) and default_val.strip():
                    single_default = default_val

                if orch_doc:
                    chosen, orch_doc_promote = self._orch_prompt_file_path(
                        field_key=key,
                        label=label,
                        help_text=doc_help,
                        default_hint=single_default,
                        show_promote=bool(mf.unrecognized),
                        attention_reason=f"document: {label}",
                    )
                    chosen = chosen.strip()
                else:
                    orch_doc_promote = False
                    chosen = self.ui.prompt_with_default(
                        f'Path to upload for "{label}"',
                        single_default,
                    ).strip()

                    while True:
                        if chosen.lstrip().startswith("["):
                            try:
                                parsed = json.loads(chosen)
                                if isinstance(parsed, list) and parsed and isinstance(parsed[0], str):
                                    chosen = parsed[0]
                            except Exception:
                                pass
                        if chosen and Path(chosen).expanduser().is_file():
                            break
                        if not chosen and not d.required:
                            chosen = ""
                            break
                        chosen = self.ui.prompt_nonempty(
                            f'Path to upload for "{label}" (must be an existing file path)'
                        ).strip()

                if not chosen:
                    continue

                chosen_path = str(Path(chosen).expanduser())
                if not Path(chosen_path).is_file():
                    raise InterruptedError(f"Document path is not a file: {chosen_path}")

                out[key] = chosen_path
                self._set_value_in_profile_db(key, chosen_path, category=mf.category)

                if chosen_path not in available_files:
                    available_files.append(chosen_path)

            if mf.category == "relative":
                self.relative_used_in_current_form = True

            if mf.unrecognized:
                do_promote = (
                    orch_doc_promote
                    if orch_doc
                    else self.ui.prompt_yes_no(
                        f'Use this document for all future "{label}" prompts (promote to absolute)?',
                        default_no=True,
                    )
                )
                if do_promote:
                    mf.category = "absolute"
                    mf.unrecognized = False
                    upsert_field(self.master, key=key, label=label, category="absolute", unrecognized=False)
                    save_master_schema(self.master, self.schema_path)
                    self._set_value_in_profile_db(key, out[key], category="absolute")

        return json.dumps(out, ensure_ascii=False)

