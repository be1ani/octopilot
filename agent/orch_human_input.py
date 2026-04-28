"""
HTTP short-poll human input against the orchestrator (Mongo-backed).

When ORCH_API_BASE and ORCH_MACHINE_ID are both set, interactive prompts use
this path. If only one is set, configuration is invalid and we exit fast.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Literal

PollBackend = Literal["orch", "terminal"]


def human_input_backend() -> PollBackend:
    base = (os.getenv("ORCH_API_BASE") or "").strip().rstrip("/")
    mid = (os.getenv("ORCH_MACHINE_ID") or "").strip()
    if bool(base) ^ bool(mid):
        raise SystemExit(
            "Invalid configuration: set both ORCH_API_BASE and ORCH_MACHINE_ID "
            "(or neither for local terminal prompts)."
        )
    return "orch" if base and mid else "terminal"


def _orch_base_mid() -> tuple[str, str]:
    human_input_backend()  # validate xor
    base = (os.getenv("ORCH_API_BASE") or "").strip().rstrip("/")
    mid = (os.getenv("ORCH_MACHINE_ID") or "").strip()
    if not base or not mid:
        raise RuntimeError("orch human input requires ORCH_API_BASE and ORCH_MACHINE_ID")
    return base, mid


def poll_interval_s(elapsed_s: float) -> float:
    """Agent poll interval; backs off to 5s for long waits."""
    e = max(0.0, float(elapsed_s))
    if e < 15.0:
        return 0.25
    if e < 30.0:
        return 0.5
    if e < 60.0:
        return 1.0
    if e < 120.0:
        return 2.0
    return 5.0


def _set_orch_attention(needed: bool, *, reason: str | None = None) -> None:
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
        with urllib.request.urlopen(req, timeout=2.0) as _resp:  # nosec - internal URL
            _ = _resp.read()
    except Exception:
        return


def _json_request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    timeout_s: float = 8.0,
) -> dict[str, Any] | None:
    base, _mid = _orch_base_mid()
    url = f"{base}{path}"
    data = None
    hdrs = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, headers=hdrs, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # nosec - internal URL
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw.strip():
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(e)
        raise RuntimeError(f"orchestrator HTTP {e.code}: {detail}") from e


def new_request_id() -> str:
    return str(uuid.uuid4())


def put_pending(request_id: str, payload: dict[str, Any]) -> None:
    _, mid = _orch_base_mid()
    _json_request(
        "PUT",
        f"/api/machines/{mid}/human-input/requests/{request_id}",
        body=payload,
        timeout_s=12.0,
    )


def get_request(request_id: str) -> dict[str, Any]:
    _, mid = _orch_base_mid()
    out = _json_request("GET", f"/api/machines/{mid}/human-input/requests/{request_id}", timeout_s=12.0)
    return out if isinstance(out, dict) else {}


def delete_request(request_id: str) -> None:
    _, mid = _orch_base_mid()
    try:
        _json_request("DELETE", f"/api/machines/{mid}/human-input/requests/{request_id}", timeout_s=6.0)
    except Exception:
        # Best-effort cleanup so the UI does not show stale cards forever.
        return


def wait_human_response(
    *,
    request_id: str,
    kind: str,
    item: dict[str, Any],
    attention_reason: str | None = None,
) -> dict[str, Any]:
    """
    Register a pending human-input request and poll until the UI answers.
    Returns the response dict (value, promote_to_absolute, force_submit, ...).
    """
    put_pending(
        request_id,
        {
            "kind": kind,
            "item": item,
        },
    )
    _set_orch_attention(True, reason=attention_reason or f"human input ({kind})")
    started = time.time()
    try:
        while True:
            doc = get_request(request_id)
            st = str(doc.get("status") or "")
            if st == "answered":
                resp = doc.get("response")
                if not isinstance(resp, dict):
                    resp = {}
                delete_request(request_id)
                return resp
            if st and st not in ("pending", "answered"):
                delete_request(request_id)
                raise RuntimeError(f"human-input session ended: {st}")
            elapsed = time.time() - started
            time.sleep(poll_interval_s(elapsed))
    finally:
        _set_orch_attention(False)


def extract_scalar_value(resp: dict[str, Any]) -> Any:
    if "value" in resp:
        return resp.get("value")
    if "confirmed" in resp:
        return bool(resp.get("confirmed"))
    if "continue" in resp:
        return bool(resp.get("continue"))
    return None


def wait_confirm(*, action_description: str) -> bool:
    rid = new_request_id()
    resp = wait_human_response(
        request_id=rid,
        kind="confirm",
        item={
            "title": "Confirm application step",
            "body": action_description,
        },
        attention_reason="confirm before submit",
    )
    return bool(resp.get("confirmed"))


def wait_captcha_continue(*, message: str) -> None:
    rid = new_request_id()
    wait_human_response(
        request_id=rid,
        kind="captcha_continue",
        item={"message": message},
        attention_reason="captcha / human verification",
    )
