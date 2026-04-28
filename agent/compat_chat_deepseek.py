"""
browser-use's ChatDeepSeek uses OpenAI's forced `tool_choice` (pin a function by name) for
structured (Pydantic) outputs. The hosted DeepSeek API rejects that *forced* `tool_choice`
shape, for example:

  400: deepseek-reasoner does not support this tool_choice

The request may use `model=deepseek-v4-flash` while the error text still says
`deepseek-reasoner` (same backend / legacy wording). The fix is the same: use
`tool_choice="auto"` and, when needed, parse JSON from the assistant `content`.

We apply this for **all** public `deepseek-…` model ids, not only `deepseek-reasoner`.

`deepseek-v4-*` models sometimes return **only the inner action parameters** at the JSON root
(e.g. `{"index": 1187}` for click, `{"seconds": 3}` for wait, `{"file_name","content"}` for
`write_file`) instead of a full `AgentOutput` with an `action: [...]` list. We detect common
browser-use and OctoPilot custom action shapes and wrap them before re-validation (see
`_infer_action_name_and_inner_dict`, `_repair_top_level_action_fragment`).

OctoPilot's `resolve_fields` / `resolve_documents` (single-key `fields` or `documents` lists) and
`ask_user_for_missing_info` (`field_path` + `question`) are included. Malformed or noisy JSON
from the model (markdown fences, trailing text) is parsed via `_json_loads_llm`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, TypeVar, overload

from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError
from browser_use.llm.deepseek.chat import ChatDeepSeek
from browser_use.llm.deepseek.serializer import DeepSeekMessageSerializer
from browser_use.llm.exceptions import ModelProviderError, ModelRateLimitError
from browser_use.llm.messages import BaseMessage
from browser_use.llm.schema import SchemaOptimizer
from browser_use.llm.views import ChatInvokeCompletion, ChatInvokeUsage

T = TypeVar("T", bound=BaseModel)


def _usage_from_openai_compatible_completion(resp: Any) -> ChatInvokeUsage | None:
    """
    Map an OpenAI-SDK ``chat.completions.create`` response to browser-use's
    :class:`ChatInvokeUsage` so token accounting / cost estimation work.

    DeepSeek reports cache hits as top-level ``prompt_cache_hit_tokens`` on
    ``usage``; OpenAI uses ``prompt_tokens_details.cached_tokens`` instead.
    """
    raw = getattr(resp, "usage", None)
    if raw is None:
        return None

    def _pick_int(obj: Any, *names: str) -> int:
        for n in names:
            v = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
            if v is None:
                continue
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
        return 0

    pt = _pick_int(raw, "prompt_tokens", "input_tokens")
    ct = _pick_int(raw, "completion_tokens", "output_tokens")
    tt = _pick_int(raw, "total_tokens")
    if not (pt or ct or tt):
        return None

    cached = 0
    pd = getattr(raw, "prompt_tokens_details", None) if not isinstance(raw, dict) else raw.get("prompt_tokens_details")
    if isinstance(pd, dict):
        cached = _pick_int(pd, "cached_tokens")
    elif pd is not None:
        cached = _pick_int(pd, "cached_tokens")

    if not cached:
        cached = _pick_int(raw, "prompt_cache_hit_tokens", "prompt_cached_tokens")

    cached_opt = int(cached) if cached else None
    if not tt and (pt or ct):
        tt = pt + ct

    return ChatInvokeUsage(
        prompt_tokens=pt,
        prompt_cached_tokens=cached_opt,
        prompt_cache_creation_tokens=None,
        prompt_image_tokens=None,
        completion_tokens=ct,
        total_tokens=tt,
    )


def _first_structural_index(s: str) -> int:
    for i, c in enumerate(s):
        if c in "[{":
            return i
    return -1


def _strip_markdown_fenced(s: str) -> str:
    t = s.strip()
    if not t.startswith("```"):
        return t
    lines = t.split("\n")
    if lines and lines[0].strip().startswith("```"):
        t = "\n".join(lines[1:])
    if "```" in t:
        t = t.rsplit("```", 1)[0]
    return t.strip()


def _extract_json_at(s: str, start: int) -> str | None:
    """Return one balanced top-level JSON value (object or array) starting at `start` (string-aware)."""
    if start < 0 or start >= len(s) or s[start] not in "[{":
        return None
    stack: list[str] = []
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            stack.append("}")
        elif c == "[":
            stack.append("]")
        elif c in "}]":
            if not stack or c != stack[-1]:
                return None
            stack.pop()
            if not stack:
                return s[start : i + 1]
    return None


def _json_candidates(s: str) -> list[str]:
    """Ordered unique candidates: raw, fenced, first balanced JSON substring."""
    out: list[str] = []
    seen: set[str] = set()
    t = s.strip() if s else ""
    for candidate in (t, _strip_markdown_fenced(t) if t else t):
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
    if not out:
        return out
    base = out[-1]
    i = _first_structural_index(base)
    if i >= 0:
        ex = _extract_json_at(base, i)
        if ex and ex not in seen:
            out.append(ex)
    return out


def unwrap_agent_output_json_blob(parsed: Any) -> Any:
    """
    Fix common malformed shapes before AgentOutput validation:

    - Extra HTML / attribute text before the JSON object (e.g. `=\"true\">{ "thinking": ...`).
    - A single `string` field whose value is JSON (or JSON with leading junk) for the real step.
    """
    if not isinstance(parsed, dict):
        return parsed
    if parsed.get("action") is not None:
        out = dict(parsed)
        out.pop("string", None)
        return out

    raw = parsed.get("string")
    if isinstance(raw, str) and raw.strip():
        s = raw.strip()
        i = _first_structural_index(s)
        if i > 0:
            s = s[i:]
        try:
            inner = _json_loads_llm(s)
        except json.JSONDecodeError:
            inner = None
        if isinstance(inner, dict) and inner.get("action") is not None:
            inner.pop("string", None)
            return inner
        mkey = raw.find('"action"')
        if mkey >= 0:
            start = raw.rfind("{", 0, mkey)
            if start >= 0:
                blob = _extract_json_at(raw, start)
                if blob:
                    try:
                        inner2 = json.loads(blob)
                    except json.JSONDecodeError:
                        inner2 = None
                    if isinstance(inner2, dict) and inner2.get("action") is not None:
                        inner2.pop("string", None)
                        return inner2

    for _k, v in parsed.items():
        if isinstance(v, str) and '"action"' in v and _k != "string":
            r = unwrap_agent_output_json_blob({"string": v})
            if isinstance(r, dict) and r.get("action") is not None:
                return r

    return parsed


def _json_loads_llm(s: str) -> Any:
    """
    Parse JSON from an LLM string: tolerate markdown code fences and leading/trailing noise by
    extracting the first full `{...}` / `[...]` when strict `json.loads` fails.
    """
    if not s or not str(s).strip():
        raise json.JSONDecodeError("Expecting value: empty or whitespace", str(s) or "", 0)
    s0 = str(s).strip()
    last: json.JSONDecodeError | None = None
    for c in _json_candidates(s0):
        try:
            return json.loads(c)
        except json.JSONDecodeError as e:
            last = e
    if last is not None:
        raise last
    raise json.JSONDecodeError("Could not parse JSON from model", s0, 0)


def _no_forced_tool_choice(model: str) -> bool:
    """
    Whether to avoid `tool_choice: {type, function: {name}}` for this model.

    Forced function choice is unsupported (or returns 400) across current DeepSeek chat models,
    including `deepseek-v4-flash` and `deepseek-reasoner` — the error body may name
    `deepseek-reasoner` even when the `model` parameter is `deepseek-v4-flash`.
    """
    m = (model or "").strip().lower()
    return m.startswith("deepseek-")


def _infer_action_name_and_inner_dict(parsed: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """
    Map a flat (wrong-level) object to a browser_use registry action name + inner param dict.
    None if the dict does not look like a single-action fragment.
    """
    keys = set(parsed)
    if not keys or "action" in parsed:
        return None
    if "index" in keys and "text" in keys:
        inner: dict[str, Any] = {
            "index": int(parsed["index"]),
            "text": str(parsed["text"]),
        }
        if "clear" in parsed:
            inner["clear"] = bool(parsed.get("clear", True))
        return "input", inner
    if "field_path" in keys and "question" in keys and keys <= {"field_path", "question", "ui"}:
        inner: dict[str, Any] = {
            "field_path": str(parsed["field_path"]),
            "question": str(parsed["question"]),
        }
        if isinstance(parsed.get("ui"), dict):
            inner["ui"] = parsed["ui"]
        return "ask_user_for_missing_info", inner
    if len(keys) == 1 and "fields" in keys and isinstance(parsed.get("fields"), list):
        return "resolve_fields", {"fields": list(parsed["fields"])}
    if len(keys) == 1 and "documents" in keys and isinstance(parsed.get("documents"), list):
        return "resolve_documents", {"documents": list(parsed["documents"])}
    # write_file( file_name, content, append=..., trailing_newline=..., leading_newline=... ) — file system
    if "file_name" in keys and "content" in keys:
        _wf_allowed = {"file_name", "content", "append", "trailing_newline", "leading_newline"}
        if keys <= _wf_allowed:
            wfin: dict[str, Any] = {
                "file_name": str(parsed["file_name"]),
                "content": str(parsed["content"]),
            }
            if "append" in keys:
                wfin["append"] = bool(parsed.get("append", False))
            if "trailing_newline" in keys:
                wfin["trailing_newline"] = bool(parsed.get("trailing_newline", True))
            if "leading_newline" in keys:
                wfin["leading_newline"] = bool(parsed.get("leading_newline", False))
            return "write_file", wfin
    if "url" in keys and keys <= {"url", "new_tab"}:
        return "navigate", {
            "url": str(parsed["url"]),
            "new_tab": bool(parsed.get("new_tab", False)),
        }
    if keys <= {"seconds"} and "seconds" in keys:
        s = parsed["seconds"]
        if isinstance(s, (float, int)):
            s_int = int(s) if float(s) == int(s) else int(round(s))
        else:
            s_int = 3
        return "wait", {"seconds": s_int}
    if ("down" in keys or "pages" in keys) and "text" not in keys and "url" not in keys:
        inner = {}
        if "down" in keys:
            inner["down"] = bool(parsed.get("down", True))
        if "pages" in keys:
            p = parsed.get("pages", 1.0)
            inner["pages"] = float(p) if isinstance(p, (int, float, str)) else 1.0
        if "index" in keys and parsed.get("index") is not None:
            inner["index"] = int(parsed["index"])
        if inner:
            return "scroll", inner
    if "index" in keys and "text" not in keys and "url" not in keys and "seconds" not in keys:
        if "down" not in keys and "pages" not in keys and keys <= {"index", "coordinate_x", "coordinate_y"}:
            out_c: dict[str, Any] = {}
            for k in ("index", "coordinate_x", "coordinate_y"):
                if k in keys and parsed.get(k) is not None:
                    v = parsed[k]
                    n = int(v) if isinstance(v, int) else int(float(v))
                    out_c[k] = n
            if out_c:
                return "click", out_c
    return None


def _fill_required_agent_output_fields(out: dict[str, Any], output_format: type[BaseModel], *, action_hint: str) -> None:
    """In-place: set required string fields on a synthetic AgentOutput dict if missing."""
    _fill: dict[str, str] = {
        "memory": f"Model returned a single-action JSON fragment; wrapped into a full {action_hint} step. Continue the task on the current page as planned.",
        "evaluation_previous_goal": "Prior model response used only action parameters; response was coerced to the required AgentOutput shape.",
        "next_goal": "Continue from the result of the wrapped action to complete the user request.",
    }
    mfields = getattr(output_format, "model_fields", None) or {}
    for fname, finfo in mfields.items():
        if fname in out or fname not in _fill:
            continue
        if not finfo.is_required():
            continue
        out[fname] = _fill[fname]
    if "memory" in mfields and (out.get("memory") in (None, "")):
        out["memory"] = _fill["memory"]


def _repair_top_level_action_fragment(
    parsed: dict[str, Any], output_format: type[BaseModel]
) -> dict[str, Any] | None:
    """
    If the model put one browser action's parameters at the JSON root, wrap as AgentOutput.
    Return None if this does not look like that mistake (caller will re-raise original error).
    """
    if "action" in parsed:
        return None
    mfields = getattr(output_format, "model_fields", None) or {}
    if "action" not in mfields:
        return None
    inf = _infer_action_name_and_inner_dict(parsed)
    if inf is None:
        return None
    aname, inner = inf
    out: dict[str, Any] = {
        "action": [{aname: inner}],
    }
    _fill_required_agent_output_fields(out, output_format, action_hint=aname)
    return out


def _validate_agent_output(
    output_format: type[BaseModel],
    parsed: Any,
) -> BaseModel:
    """Model validate with one repair pass for partial DeepSeek / AgentOutput JSON (see module doc)."""
    if isinstance(parsed, dict):
        parsed = unwrap_agent_output_json_blob(parsed)
    if output_format is None or not isinstance(parsed, dict):
        return output_format.model_validate(parsed)  # type: ignore[union-attr]
    try:
        return output_format.model_validate(parsed)
    except ValidationError as err:
        fixed = _repair_top_level_action_fragment(parsed, output_format)
        if fixed is None:
            raise err
        return output_format.model_validate(fixed)


@dataclass
class OctopilotChatDeepSeek(ChatDeepSeek):
    """
    Like browser_use's ChatDeepSeek, but for all `deepseek-…` models the structured-output path
    uses `tool_choice=auto` (not forced function) and can parse Pydantic-shaped JSON from
    `message.content` if the API returns no `tool_calls`.
    """

    @overload
    async def ainvoke(
        self,
        messages: list[BaseMessage],
        output_format: None = None,
        tools: list[dict[str, Any]] | None = None,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> ChatInvokeCompletion[str]: ...

    @overload
    async def ainvoke(
        self,
        messages: list[BaseMessage],
        output_format: type[T],
        tools: list[dict[str, Any]] | None = None,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> ChatInvokeCompletion[T]: ...

    async def ainvoke(  # type: ignore[override]
        self,
        messages: list[BaseMessage],
        output_format: type[T] | None = None,
        tools: list[dict[str, Any]] | None = None,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> ChatInvokeCompletion[T] | ChatInvokeCompletion[str]:
        client = self._client()
        ds_messages = DeepSeekMessageSerializer.serialize_messages(messages)
        common: dict[str, Any] = {}

        if self.temperature is not None:
            common["temperature"] = self.temperature
        if self.max_tokens is not None:
            common["max_tokens"] = self.max_tokens
        if self.top_p is not None:
            common["top_p"] = self.top_p
        if self.seed is not None:
            common["seed"] = self.seed

        if self.base_url and str(self.base_url).endswith("/beta"):
            if ds_messages and isinstance(ds_messages[-1], dict) and ds_messages[-1].get("role") == "assistant":
                ds_messages[-1]["prefix"] = True
            if stop:
                common["stop"] = stop

        if output_format is None and not tools:
            try:
                resp = await client.chat.completions.create(  # type: ignore
                    model=self.model,
                    messages=ds_messages,  # type: ignore
                    **common,
                )
                usage = _usage_from_openai_compatible_completion(resp)
                fr = None
                try:
                    fr = resp.choices[0].finish_reason  # type: ignore[index]
                except Exception:
                    fr = None
                return ChatInvokeCompletion(
                    completion=resp.choices[0].message.content or "",
                    usage=usage,
                    stop_reason=fr,
                )
            except RateLimitError as e:
                raise ModelRateLimitError(str(e), model=self.name) from e
            except (APIError, APIConnectionError, APITimeoutError, APIStatusError) as e:
                raise ModelProviderError(str(e), model=self.name) from e
            except Exception as e:
                raise ModelProviderError(str(e), model=self.name) from e

        if tools or (output_format is not None and hasattr(output_format, "model_json_schema")):
            try:
                call_tools = tools
                tool_choice: Any = None
                if output_format is not None and hasattr(output_format, "model_json_schema"):
                    tool_name = output_format.__name__
                    schema = SchemaOptimizer.create_optimized_json_schema(output_format)
                    schema.pop("title", None)
                    call_tools = [
                        {
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "description": f"Return a JSON object of type {tool_name}",
                                "parameters": schema,
                            },
                        }
                    ]
                    if _no_forced_tool_choice(self.model):
                        tool_choice = "auto"
                    else:
                        tool_choice = {
                            "type": "function",
                            "function": {"name": tool_name},
                        }
                resp = await client.chat.completions.create(  # type: ignore
                    model=self.model,
                    messages=ds_messages,  # type: ignore
                    tools=call_tools,  # type: ignore
                    tool_choice=tool_choice,  # type: ignore
                    **common,
                )
                usage = _usage_from_openai_compatible_completion(resp)
                fr = None
                try:
                    fr = resp.choices[0].finish_reason  # type: ignore[index]
                except Exception:
                    fr = None
                msg = resp.choices[0].message
                if not msg.tool_calls and output_format is not None and hasattr(
                    output_format, "model_json_schema"
                ):
                    c = (msg.content or "").strip()
                    if c:
                        try:
                            completion = _validate_agent_output(
                                output_format, _json_loads_llm(c)
                            )
                        except json.JSONDecodeError as e:
                            raise ModelProviderError(str(e), model=self.name) from e
                        return ChatInvokeCompletion(
                            completion=completion,
                            usage=usage,
                            stop_reason=fr,
                        )
                if not msg.tool_calls:
                    raise ValueError("Expected tool_calls in response but got none")
                raw_args = msg.tool_calls[0].function.arguments
                if isinstance(raw_args, str):
                    try:
                        parsed = _json_loads_llm(raw_args)
                    except json.JSONDecodeError as e:
                        raise ModelProviderError(str(e), model=self.name) from e
                else:
                    parsed = raw_args
                if output_format is not None:
                    return ChatInvokeCompletion(
                        completion=_validate_agent_output(output_format, parsed),
                        usage=usage,
                        stop_reason=fr,
                    )
                return ChatInvokeCompletion(
                    completion=parsed,
                    usage=usage,
                    stop_reason=fr,
                )
            except RateLimitError as e:
                raise ModelRateLimitError(str(e), model=self.name) from e
            except (APIError, APIConnectionError, APITimeoutError, APIStatusError) as e:
                raise ModelProviderError(str(e), model=self.name) from e
            except Exception as e:
                raise ModelProviderError(str(e), model=self.name) from e

        if output_format is not None and hasattr(output_format, "model_json_schema"):
            try:
                resp = await client.chat.completions.create(  # type: ignore
                    model=self.model,
                    messages=ds_messages,  # type: ignore
                    response_format={"type": "json_object"},
                    **common,
                )
                content = resp.choices[0].message.content
                if not content:
                    raise ModelProviderError("Empty JSON content in DeepSeek response", model=self.name)
                try:
                    comp = _validate_agent_output(output_format, _json_loads_llm(content))
                except json.JSONDecodeError as e:
                    raise ModelProviderError(str(e), model=self.name) from e
                usage = _usage_from_openai_compatible_completion(resp)
                fr = None
                try:
                    fr = resp.choices[0].finish_reason  # type: ignore[index]
                except Exception:
                    fr = None
                return ChatInvokeCompletion(
                    completion=comp,
                    usage=usage,
                    stop_reason=fr,
                )
            except RateLimitError as e:
                raise ModelRateLimitError(str(e), model=self.name) from e
            except (APIError, APIConnectionError, APITimeoutError, APIStatusError) as e:
                raise ModelProviderError(str(e), model=self.name) from e
            except Exception as e:
                raise ModelProviderError(str(e), model=self.name) from e

        raise ModelProviderError("No valid ainvoke execution path for DeepSeek LLM", model=self.name)
