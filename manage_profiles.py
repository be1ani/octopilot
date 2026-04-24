#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional

from profiles.models import BaseInfo, OtherInfo, Profile
from profiles.from_pdf import build_profile_json_from_pdf
from profiles.pdf_extract import extract_text_from_pdf
from profiles.store import ProfileStore


def _new_profile_id() -> str:
    return uuid.uuid4().hex


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


def cmd_import_pdf(args: argparse.Namespace) -> int:
    text = extract_text_from_pdf(args.pdf)
    excerpt = text[:2000] if text else ""

    profile_id = args.profile_id or _new_profile_id()
    now = datetime.now(UTC)

    if args.no_llm:
        # Minimal profile; you can enrich later with LLM.
        profile = Profile(
            profile_id=profile_id,
            profile_type=args.profile_type,
            label=args.label,
            base=BaseInfo(full_name=args.full_name),
            other=OtherInfo(),
            source_pdf_path=str(Path(args.pdf)),
            created_at=now,
            updated_at=now,
        )
    else:
        from profiles.llm_profile import build_profile_from_text_with_llm

        profile = build_profile_from_text_with_llm(
            resume_text=text,
            profile_id=profile_id,
            profile_type=args.profile_type,
            label=args.label,
            source_pdf_path=str(Path(args.pdf)),
            model=args.model,
        )
        profile.source_pdf_path = str(Path(args.pdf))

    store = ProfileStore(args.db)
    store.upsert_profile(profile)
    print(json.dumps({"ok": True, "profile_id": profile.profile_id}, ensure_ascii=False))
    return 0


def cmd_pdf_to_json(args: argparse.Namespace) -> int:
    profile_id = args.profile_id or _new_profile_id()
    obj = build_profile_json_from_pdf(
        pdf_path=args.pdf,
        profile_id=profile_id,
        profile_type=args.profile_type,
        label=args.label,
        model=args.model,
        no_llm=bool(args.no_llm),
        full_name=str(args.full_name or "Unknown"),
    )
    # Print only the profile JSON object (no wrapper) so it can be pasted into the UI Import JSON box.
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    store = ProfileStore(args.db)
    profiles = store.load()
    out = [
        {
            "profile_id": p.profile_id,
            "profile_type": p.profile_type,
            "label": p.label,
            "full_name": p.base.full_name,
            "updated_at": p.updated_at,
        }
        for p in sorted(profiles.values(), key=lambda x: x.profile_id)
    ]
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = ProfileStore(args.db)
    profiles = store.load()
    profile = profiles.get(args.profile_id)
    if not profile:
        raise SystemExit(f"Profile not found: {args.profile_id}")
    print(json.dumps(profile.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    _load_dotenv()
    p = argparse.ArgumentParser(description="Manage job-application applicant profiles.")
    p.add_argument("--db", default="profiles_db.json", help="Path to profiles DB JSON file.")
    sub = p.add_subparsers(dest="cmd", required=True)

    ip = sub.add_parser("import-pdf", help="Parse a PDF resume and store a profile.")
    ip.add_argument("--profile-type", required=True, help='e.g. "web_developer"')
    ip.add_argument("--label", default=None)
    ip.add_argument("--profile-id", default=None, help="Optional explicit profile_id; otherwise auto-generated.")
    ip.add_argument("--pdf", required=True, help="Path to resume PDF.")
    ip.add_argument("--model", default=None, help="Override LLM model (else PROFILE_LLM_MODEL).")
    ip.add_argument("--no-llm", action="store_true", help="Do not call LLM; store minimal profile.")
    ip.add_argument(
        "--full-name",
        default="Unknown",
        help='Only used with --no-llm. (Example: "Jane Doe")',
    )
    ip.set_defaults(func=cmd_import_pdf)

    pj = sub.add_parser("pdf-to-json", help="Parse a PDF resume and print a Profile JSON object (for UI import).")
    pj.add_argument("--profile-type", required=True, help='e.g. "web_developer"')
    pj.add_argument("--label", default=None)
    pj.add_argument("--profile-id", default=None, help="Optional explicit profile_id; otherwise auto-generated.")
    pj.add_argument("--pdf", required=True, help="Path to resume PDF.")
    pj.add_argument("--model", default=None, help="Override LLM model (else PROFILE_LLM_MODEL).")
    pj.add_argument("--no-llm", action="store_true", help="Do not call LLM; output minimal profile.")
    pj.add_argument(
        "--full-name",
        default="Unknown",
        help='Only used with --no-llm. (Example: "Jane Doe")',
    )
    pj.set_defaults(func=cmd_pdf_to_json)

    ls = sub.add_parser("list", help="List applicants and their profiles.")
    ls.set_defaults(func=cmd_list)

    sh = sub.add_parser("show", help="Show a single stored profile.")
    sh.add_argument("--profile-id", required=True)
    sh.set_defaults(func=cmd_show)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

