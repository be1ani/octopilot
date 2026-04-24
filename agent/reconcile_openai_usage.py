"""
Reconcile the local LLM cost ledger against OpenAI's Usage / Costs APIs.

Why
---
Provider-side numbers are the ground truth. This script cross-checks the
JSONL ledger written by :class:`agent.llm_usage.LLMUsageRecorder` against
OpenAI's organization-level Usage and Costs endpoints so drift is visible.

Usage
-----
    python -m agent.reconcile_openai_usage \\
        --ledger orchestrator/data/llm_ledger.jsonl \\
        --start 2026-04-01 --end 2026-04-19

Requires an **admin** API key in ``OPENAI_ADMIN_KEY`` (an org-scoped admin
key; the regular ``OPENAI_API_KEY`` cannot read usage/costs). Docs:
https://platform.openai.com/docs/api-reference/usage
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, Dict, Iterable, List, Optional

import urllib.error
import urllib.parse
import urllib.request


_USAGE_BASE = "https://api.openai.com/v1/organization/usage"
_COSTS_URL = "https://api.openai.com/v1/organization/costs"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _http_get(url: str, *, admin_key: str, timeout: float = 30.0) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {admin_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - trusted URL
        body = resp.read().decode("utf-8")
    return json.loads(body)


def _iter_pages(base_url: str, params: Dict[str, Any], *, admin_key: str) -> Iterable[Dict[str, Any]]:
    """Iterate through every result page of an OpenAI usage/cost endpoint."""
    next_page: Optional[str] = None
    while True:
        q = dict(params)
        if next_page:
            q["page"] = next_page
        url = f"{base_url}?{urllib.parse.urlencode(q, doseq=True)}"
        try:
            doc = _http_get(url, admin_key=admin_key)
        except urllib.error.HTTPError as e:
            raise SystemExit(f"OpenAI API error {e.code}: {e.read().decode('utf-8', 'replace')}") from e
        for bucket in doc.get("data") or []:
            yield bucket
        next_page = doc.get("next_page") if doc.get("has_more") else None
        if not next_page:
            return


# ---------------------------------------------------------------------------
# Ledger parsing
# ---------------------------------------------------------------------------


def _normalize_model(m: str) -> str:
    # Keep in sync with agent/llm_usage.py:_normalize_model_name
    if not m:
        return ""
    base = m.split("/", 1)[-1]
    if base.startswith("ft:"):
        try:
            base = base.split(":", 2)[1] or base
        except IndexError:
            pass
    parts = base.split("-")
    if len(parts) >= 5 and parts[-3].isdigit() and parts[-2].isdigit() and parts[-1].isdigit():
        base = "-".join(parts[:-3])
    return base


def _load_ledger(path: str, *, start: datetime, end: datetime) -> Dict[tuple[str, str], Dict[str, float]]:
    """
    Aggregate ledger rows in ``[start, end)`` by ``(date, model)``.

    Returns per-bucket dict with input, output, total, cost_usd.
    """
    out: Dict[tuple[str, str], Dict[str, float]] = defaultdict(
        lambda: {"input_tokens": 0.0, "output_tokens": 0.0, "total_tokens": 0.0, "cost_usd": 0.0}
    )
    try:
        f = open(path, "r", encoding="utf-8")
    except FileNotFoundError:
        raise SystemExit(f"Ledger not found: {path}")
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_raw = row.get("ts")
            if not isinstance(ts_raw, str):
                continue
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts < start or ts >= end:
                continue
            day = ts.astimezone(UTC).date().isoformat()
            model = _normalize_model(str(row.get("model") or ""))
            key = (day, model)
            usage = row.get("usage") or {}
            out[key]["input_tokens"] += float(usage.get("input_tokens") or 0)
            out[key]["output_tokens"] += float(usage.get("output_tokens") or 0)
            out[key]["total_tokens"] += float(usage.get("total_tokens") or 0)
            out[key]["cost_usd"] += float(row.get("cost_usd") or 0)
    return out


# ---------------------------------------------------------------------------
# OpenAI side
# ---------------------------------------------------------------------------


def _fetch_openai_usage(*, admin_key: str, start: datetime, end: datetime) -> Dict[tuple[str, str], Dict[str, float]]:
    """
    Pull OpenAI-reported usage bucketed per day per model.

    Covers both chat/completions and embeddings endpoints.
    """
    out: Dict[tuple[str, str], Dict[str, float]] = defaultdict(
        lambda: {"input_tokens": 0.0, "output_tokens": 0.0, "total_tokens": 0.0}
    )
    params = {
        "start_time": int(start.timestamp()),
        "end_time": int(end.timestamp()),
        "bucket_width": "1d",
        "group_by": ["model"],
        "limit": 31,
    }
    for endpoint in ("completions", "embeddings"):
        for bucket in _iter_pages(f"{_USAGE_BASE}/{endpoint}", params, admin_key=admin_key):
            bucket_start = bucket.get("start_time")
            if not isinstance(bucket_start, (int, float)):
                continue
            day = datetime.fromtimestamp(float(bucket_start), tz=UTC).date().isoformat()
            for result in bucket.get("results") or []:
                model = _normalize_model(str(result.get("model") or ""))
                it = float(result.get("input_tokens") or 0)
                ot = float(result.get("output_tokens") or 0)
                # embeddings endpoint reports only input_tokens.
                tt = it + ot
                slot = out[(day, model)]
                slot["input_tokens"] += it
                slot["output_tokens"] += ot
                slot["total_tokens"] += tt
    return out


def _fetch_openai_costs(*, admin_key: str, start: datetime, end: datetime) -> Dict[tuple[str, str], float]:
    """Pull OpenAI-reported USD cost bucketed per day per (line_item ≈ model)."""
    out: Dict[tuple[str, str], float] = defaultdict(float)
    params = {
        "start_time": int(start.timestamp()),
        "end_time": int(end.timestamp()),
        "bucket_width": "1d",
        "group_by": ["line_item"],
        "limit": 31,
    }
    for bucket in _iter_pages(_COSTS_URL, params, admin_key=admin_key):
        bucket_start = bucket.get("start_time")
        if not isinstance(bucket_start, (int, float)):
            continue
        day = datetime.fromtimestamp(float(bucket_start), tz=UTC).date().isoformat()
        for result in bucket.get("results") or []:
            line_item = str(result.get("line_item") or "")
            model = _normalize_model(line_item.split(",", 1)[0] if line_item else "")
            amount = result.get("amount") or {}
            out[(day, model)] += float(amount.get("value") or 0)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_date(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s).replace(tzinfo=UTC)
    except ValueError:
        raise SystemExit(f"invalid date: {s!r} (expected YYYY-MM-DD)")


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", required=False, default=os.getenv("AGENT_LLM_LEDGER_PATH") or "")
    ap.add_argument("--start", required=True, help="UTC date YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", required=True, help="UTC date YYYY-MM-DD (exclusive)")
    ap.add_argument("--threshold-pct", type=float, default=1.0, help="alert on abs(drift) > this percent")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON only")
    args = ap.parse_args(argv)

    if not args.ledger:
        print("--ledger is required (or set AGENT_LLM_LEDGER_PATH)", file=sys.stderr)
        return 2

    admin_key = os.getenv("OPENAI_ADMIN_KEY") or ""
    if not admin_key:
        print("OPENAI_ADMIN_KEY is not set (required for Usage/Costs API)", file=sys.stderr)
        return 2

    start = _parse_date(args.start)
    end = _parse_date(args.end)
    if end <= start:
        print("--end must be after --start", file=sys.stderr)
        return 2

    local = _load_ledger(args.ledger, start=start, end=end)
    remote_usage = _fetch_openai_usage(admin_key=admin_key, start=start, end=end)
    remote_costs = _fetch_openai_costs(admin_key=admin_key, start=start, end=end)

    all_keys = set(local.keys()) | set(remote_usage.keys()) | set(remote_costs.keys())

    rows = []
    totals = {
        "local_tokens": 0.0,
        "remote_tokens": 0.0,
        "local_cost": 0.0,
        "remote_cost": 0.0,
    }
    for key in sorted(all_keys):
        day, model = key
        L = local.get(key, {})
        R_u = remote_usage.get(key, {})
        R_c = remote_costs.get(key, 0.0)
        loc_t = float(L.get("total_tokens") or 0)
        rem_t = float(R_u.get("total_tokens") or 0)
        loc_c = float(L.get("cost_usd") or 0)
        rem_c = float(R_c)
        totals["local_tokens"] += loc_t
        totals["remote_tokens"] += rem_t
        totals["local_cost"] += loc_c
        totals["remote_cost"] += rem_c
        tok_drift_pct = _pct(loc_t - rem_t, rem_t)
        cost_drift_pct = _pct(loc_c - rem_c, rem_c)
        rows.append(
            {
                "day": day,
                "model": model,
                "local_total_tokens": loc_t,
                "openai_total_tokens": rem_t,
                "tokens_drift_pct": tok_drift_pct,
                "local_cost_usd": round(loc_c, 6),
                "openai_cost_usd": round(rem_c, 6),
                "cost_drift_pct": cost_drift_pct,
            }
        )

    result = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rows": rows,
        "totals": {
            "local_tokens": int(totals["local_tokens"]),
            "openai_tokens": int(totals["remote_tokens"]),
            "tokens_drift_pct": _pct(totals["local_tokens"] - totals["remote_tokens"], totals["remote_tokens"]),
            "local_cost_usd": round(totals["local_cost"], 6),
            "openai_cost_usd": round(totals["remote_cost"], 6),
            "cost_drift_pct": _pct(totals["local_cost"] - totals["remote_cost"], totals["remote_cost"]),
        },
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_human(result)

    # Exit non-zero when drift exceeds threshold so this can be used in CI.
    tot = result["totals"]
    worst = max(
        abs(tot["tokens_drift_pct"] or 0.0),
        abs(tot["cost_drift_pct"] or 0.0),
    )
    return 0 if worst <= args.threshold_pct else 1


def _pct(diff: float, base: float) -> Optional[float]:
    if base == 0:
        return None if diff == 0 else float("inf")
    return round((diff / base) * 100.0, 3)


def _print_human(result: Dict[str, Any]) -> None:
    print(f"Range: {result['start']} .. {result['end']}")
    print(f"{'day':<11}{'model':<28}{'local_tok':>12}{'openai_tok':>12}{'drift%':>9}"
          f"{'local_$':>10}{'openai_$':>10}{'drift%':>9}")
    for r in result["rows"]:
        print(
            f"{r['day']:<11}{(r['model'] or '-'):<28}"
            f"{int(r['local_total_tokens']):>12}{int(r['openai_total_tokens']):>12}"
            f"{_fmt_pct(r['tokens_drift_pct']):>9}"
            f"{r['local_cost_usd']:>10.4f}{r['openai_cost_usd']:>10.4f}"
            f"{_fmt_pct(r['cost_drift_pct']):>9}"
        )
    t = result["totals"]
    print("-" * 99)
    print(
        f"{'TOTAL':<39}{t['local_tokens']:>12}{t['openai_tokens']:>12}"
        f"{_fmt_pct(t['tokens_drift_pct']):>9}"
        f"{t['local_cost_usd']:>10.4f}{t['openai_cost_usd']:>10.4f}"
        f"{_fmt_pct(t['cost_drift_pct']):>9}"
    )


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "  n/a"
    if v == float("inf"):
        return "  inf"
    return f"{v:+.2f}"


if __name__ == "__main__":
    sys.exit(main())
