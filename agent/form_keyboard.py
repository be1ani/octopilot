"""
Keyboard-first form navigation helpers for the browser agent.

Dispatches Playwright key events and returns a structured focus snapshot
(via CDP Runtime.evaluate) so the model can reason about focus without
relying on volatile element indices alone.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- Pydantic (agent tool schema) -------------------------------------------------

FormKeyboardKey = Literal[
    "tab",
    "shift_tab",
    "enter",
    "escape",
    "space",
    "arrow_down",
    "arrow_up",
    "arrow_left",
    "arrow_right",
    "focus_first_form_field",
]


class FormKeyboardParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: FormKeyboardKey = Field(
        ...,
        description=(
            "Keyboard navigation: tab/shift_tab move focus between fields; "
            "arrow_* moves within many listbox/combobox widgets once expanded; "
            "enter activates/selects; escape closes; space toggles buttons/checkboxes. "
            "focus_first_form_field focuses the first visible control inside a <form> (fallback: page-wide)."
        ),
    )
    repeat: int = Field(
        default=1,
        ge=1,
        le=25,
        description="How many times to send the key (ignored for focus_first_form_field).",
    )


# --- JavaScript -------------------------------------------------------------------

_FOCUS_SNAPSHOT_JS = r"""
(() => {
  function textOfRefIds(ref) {
    if (!ref) return '';
    return String(ref).trim().split(/\s+/).map(function (id) {
      if (!id) return '';
      var n = document.getElementById(id);
      return n ? String(n.textContent || '').trim() : '';
    }).filter(Boolean).join(' | ');
  }

  function deepActiveElement() {
    var a = document.activeElement;
    if (!a) return null;
    while (a && a.shadowRoot && a.shadowRoot.activeElement) {
      a = a.shadowRoot.activeElement;
    }
    return a;
  }

  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    var r = el.getBoundingClientRect();
    if (!r || (r.width <= 0 && r.height <= 0)) return false;
    var st = window.getComputedStyle(el);
    return st && st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
  }

  var el = deepActiveElement();
  if (!el || el === document.body) {
    return { focused: false, reason: 'body_or_none', tag: el ? 'BODY' : 'NONE' };
  }

  var tag = (el.tagName || '').toUpperCase();
  var type = (el.getAttribute && el.getAttribute('type')) || '';
  var role = (el.getAttribute && el.getAttribute('role')) || '';
  var labels = '';
  try {
    if (el.labels && el.labels.length) {
      labels = Array.prototype.slice.call(el.labels).map(function (l) {
        return String(l.textContent || '').trim();
      }).filter(Boolean).join(' | ');
    }
  } catch (_e) {}

  var ariaLabel = (el.getAttribute && el.getAttribute('aria-label')) || '';
  var name = (el.getAttribute && el.getAttribute('name')) || '';
  var id = (el.getAttribute && el.getAttribute('id')) || '';
  var ariaBy = textOfRefIds(el.getAttribute && el.getAttribute('aria-labelledby'));
  var placeholder = (el.getAttribute && el.getAttribute('placeholder')) || '';
  var ariaExpanded = el.getAttribute && el.getAttribute('aria-expanded');

  var valuePreview = '';
  if (tag === 'SELECT') {
    try {
      var o = el.selectedOptions && el.selectedOptions[0];
      valuePreview = o ? String(o.textContent || '').trim() : '';
    } catch (_e2) {}
  } else if ('value' in el && el.value != null && el.value !== undefined) {
    var v = String(el.value);
    valuePreview = v.length > 200 ? v.slice(0, 197) + '...' : v;
  } else {
    var t = String(el.textContent || '').trim();
    valuePreview = t.length > 120 ? t.slice(0, 117) + '...' : t;
  }

  var activeDescendant = null;
  var adId = el.getAttribute && el.getAttribute('aria-activedescendant');
  if (adId) {
    var node = document.getElementById(adId);
    if (node) {
      activeDescendant = {
        id: adId,
        text: String(node.textContent || '').trim().slice(0, 240),
        role: (node.getAttribute && node.getAttribute('role')) || ''
      };
    }
  }

  var rect = el.getBoundingClientRect && el.getBoundingClientRect();
  return {
    focused: true,
    tag: tag,
    type: type,
    role: role,
    name: name,
    id: id,
    labels: labels,
    aria_label: ariaLabel,
    aria_labelledby_text: ariaBy,
    placeholder: placeholder,
    aria_expanded: ariaExpanded,
    value_preview: valuePreview,
    aria_active_descendant: activeDescendant,
    visible: visible(el),
    rect: rect ? { x: rect.x, y: rect.y, w: rect.width, h: rect.height } : null
  };
})()
"""

_FOCUS_FIRST_FIELD_JS = r"""
(() => {
  var selectors = [
    'form input:not([type="hidden"]):not([disabled])',
    'form textarea:not([disabled])',
    'form select:not([disabled])',
    'form [role="combobox"]:not([aria-disabled="true"])',
    'form [contenteditable="true"]:not([aria-disabled="true"])'
  ];
  var nodes = [];
  for (var s = 0; s < selectors.length; s++) {
    try {
      nodes = nodes.concat(Array.prototype.slice.call(document.querySelectorAll(selectors[s])));
    } catch (_e) {}
  }
  if (!nodes.length) {
    var fallback = 'input:not([type="hidden"]):not([disabled]), textarea:not([disabled]), select:not([disabled]), [role="combobox"]';
    try {
      nodes = Array.prototype.slice.call(document.querySelectorAll(fallback));
    } catch (_e2) {
      nodes = [];
    }
  }

  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    var r = el.getBoundingClientRect();
    if (!r || (r.width <= 0 && r.height <= 0)) return false;
    var st = window.getComputedStyle(el);
    return st && st.visibility !== 'hidden' && st.display !== 'none';
  }

  for (var i = 0; i < nodes.length; i++) {
    var n = nodes[i];
    if (!visible(n)) continue;
    try {
      n.focus({ preventScroll: false });
      return {
        ok: true,
        tag: (n.tagName || '').toUpperCase(),
        role: (n.getAttribute && n.getAttribute('role')) || '',
        name: (n.getAttribute && n.getAttribute('name')) || '',
        id: (n.getAttribute && n.getAttribute('id')) || ''
      };
    } catch (_e3) {}
  }
  return { ok: false, reason: 'no_visible_form_control' };
})()
"""

# Playwright key names
_KEY_TO_PLAYWRIGHT: dict[str, str] = {
    "tab": "Tab",
    "shift_tab": "Shift+Tab",
    "enter": "Enter",
    "escape": "Escape",
    "space": "Space",
    "arrow_down": "ArrowDown",
    "arrow_up": "ArrowUp",
    "arrow_left": "ArrowLeft",
    "arrow_right": "ArrowRight",
}


def _key_delay_s() -> float:
    raw = (os.getenv("AGENT_FORM_KEYBOARD_KEY_DELAY_MS") or "55").strip()
    try:
        return max(0.0, float(raw) / 1000.0)
    except ValueError:
        return 0.055


def _cdp_eval_extract_value(resp: Any) -> Any:
    """Best-effort parse of CDP Runtime.evaluate response."""
    if resp is None:
        return None
    if not isinstance(resp, dict):
        try:
            outer = getattr(resp, "result", None)
            if isinstance(outer, dict):
                inner = outer.get("result")
                if isinstance(inner, dict) and "value" in inner:
                    return inner.get("value")
        except Exception:
            pass
        return None
    outer = resp.get("result")
    if not isinstance(outer, dict):
        return None
    if outer.get("exceptionDetails"):
        return None
    inner = outer.get("result")
    if isinstance(inner, dict) and "value" in inner:
        return inner.get("value")
    # Some clients nest differently
    if "value" in outer:
        return outer.get("value")
    return None


async def _run_eval(browser_session: Any, expression: str) -> Any:
    cdp_session = await browser_session.get_or_create_cdp_session()
    resp = await asyncio.wait_for(
        cdp_session.cdp_client.send.Runtime.evaluate(
            params={
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": False,
            },
            session_id=cdp_session.session_id,
        ),
        timeout=3.0,
    )
    return _cdp_eval_extract_value(resp)


async def _get_playwright_page(browser_session: Any) -> Any:
    for name in ("must_get_current_page", "get_current_page", "get_active_page", "get_page"):
        fn = getattr(browser_session, name, None)
        if not callable(fn):
            continue
        try:
            page = await fn()
            if page is not None:
                return page
        except Exception:
            continue
    return None


async def execute_form_keyboard(
    browser_session: Any,
    *,
    key: str,
    repeat: int,
) -> tuple[bool, dict[str, Any], str | None]:
    """
    Run one logical keyboard step and return (success, payload, error).

    `payload` always includes `focus` (snapshot dict) on success paths.
    """
    keys_sent: list[str] = []
    if browser_session is None:
        return False, {}, "browser_session missing"

    if key == "focus_first_form_field":
        first = await _run_eval(browser_session, _FOCUS_FIRST_FIELD_JS)
        if not isinstance(first, dict):
            first = {"ok": False, "reason": "eval_unexpected"}
        snap = await _run_eval(browser_session, _FOCUS_SNAPSHOT_JS)
        if not isinstance(snap, dict):
            snap = {"focused": False, "reason": "snapshot_failed"}
        payload: dict[str, Any] = {
            "keys_sent": [],
            "focus_first": first,
            "focus": snap,
        }
        if not first.get("ok"):
            return True, payload, None  # still success tool-wise; model reads ok:false
        return True, payload, None

    pw = _KEY_TO_PLAYWRIGHT.get(key)
    if not pw:
        return False, {}, f"unknown key {key!r}"

    page = await _get_playwright_page(browser_session)
    if page is None:
        return False, {}, "could not resolve Playwright page for keyboard.press"

    delay = _key_delay_s()
    try:
        kb = getattr(page, "keyboard", None)
        if kb is None:
            return False, {}, "page.keyboard not available"
        for _ in range(int(repeat)):
            await kb.press(pw)
            keys_sent.append(pw)
            if delay > 0:
                await asyncio.sleep(delay)
    except Exception as e:
        return False, {"keys_sent": keys_sent}, str(e)

    snap = await _run_eval(browser_session, _FOCUS_SNAPSHOT_JS)
    if not isinstance(snap, dict):
        snap = {"focused": False, "reason": "snapshot_failed"}
    return True, {"keys_sent": keys_sent, "focus": snap}, None
