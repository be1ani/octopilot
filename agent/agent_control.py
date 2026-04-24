"""
Cooperative pause / takeover control for the agent.

The orchestrator bind-mounts a host directory into the agent container and
writes ``state.json`` with one of three states:

* ``running``  — normal operation.
* ``paused``   — block before the next LLM call; wake up when flipped back
  to ``running``.
* ``stopping`` — raise :class:`TakeoverRequested` so the agent exits cleanly
  and lets the human take over the desktop.

Why file-based rather than SIGSTOP?
-----------------------------------
``docker exec ... kill -STOP`` is unreliable in this codebase: ``pgrep`` may
miss the agent PID because it lives inside ``tmux``, ``docker exec`` can
fail silently, and SIGSTOP doesn't interrupt in-flight HTTP calls. A file
watched by the agent itself is cooperative, race-free, and survives process
forks.

The file is read on every call to :meth:`AgentControl.gate` (or
:meth:`gate_async`). The gate blocks while ``paused`` and raises
:class:`TakeoverRequested` while ``stopping`` — which is a ``SystemExit``
subclass with a dedicated exit code so the outer entrypoint can distinguish
a takeover from a normal finish.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

__all__ = [
    "AgentControl",
    "TakeoverRequested",
    "TAKEOVER_EXIT_CODE",
    "get_default",
]

_logger = logging.getLogger(__name__)

_DEFAULT_DIR_ENV = "AGENT_CONTROL_DIR"
_FILE_NAME = "state.json"
_DEFAULT_POLL_S = 1.0

TAKEOVER_EXIT_CODE = 42


class TakeoverRequested(SystemExit):
    """
    Raised when the orchestrator asks the agent to stop so the user can
    take over the desktop. Subclass of ``SystemExit`` so it unwinds the
    stack cleanly and is not swallowed by broad ``except Exception``.
    """

    def __init__(self, reason: str = "takeover requested by orchestrator") -> None:
        super().__init__(TAKEOVER_EXIT_CODE)
        self.reason = reason

    def __str__(self) -> str:
        return self.reason


class AgentControl:
    """
    Watches the orchestrator's control file and exposes a guard.

    Call :meth:`gate` (sync) or :meth:`gate_async` right before issuing an
    LLM request. The guard returns immediately when the state is
    ``running``, blocks while it is ``paused``, and raises
    :class:`TakeoverRequested` when it is ``stopping``.
    """

    def __init__(
        self,
        *,
        directory: str | os.PathLike | None = None,
        poll_interval_s: float = _DEFAULT_POLL_S,
    ) -> None:
        raw_dir = directory if directory is not None else os.getenv(_DEFAULT_DIR_ENV)
        self._dir = Path(raw_dir) if raw_dir else None
        self._poll_interval_s = max(0.1, float(poll_interval_s))
        self._lock = threading.Lock()
        self._last_state: str = "running"
        self._last_state_at: float = time.time()
        self._listeners: list[Callable[[str], None]] = []

    # ------------------------------------------------------------------ info

    @property
    def enabled(self) -> bool:
        return self._dir is not None and str(self._dir).strip() != ""

    @property
    def state(self) -> str:
        return self._last_state

    @property
    def state_path(self) -> Optional[Path]:
        if self._dir is None:
            return None
        return self._dir / _FILE_NAME

    def add_listener(self, cb: Callable[[str], None]) -> None:
        """Invoke ``cb(new_state)`` whenever the observed state changes."""
        with self._lock:
            self._listeners.append(cb)

    # -------------------------------------------------------------- internals

    def _read_state(self) -> str:
        path = self.state_path
        if path is None:
            return "running"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return "running"
        except (OSError, json.JSONDecodeError) as exc:
            _logger.debug("agent_control: failed to read %s: %s", path, exc)
            return self._last_state
        s = doc.get("state") if isinstance(doc, dict) else None
        if s in ("running", "paused", "stopping"):
            return s
        return "running"

    def _set_state(self, new_state: str) -> None:
        if new_state == self._last_state:
            return
        prev = self._last_state
        self._last_state = new_state
        self._last_state_at = time.time()
        _logger.info("agent_control: state %s -> %s", prev, new_state)
        listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(new_state)
            except Exception:
                # A misbehaving listener must not break the control path.
                _logger.exception("agent_control: listener raised for %s", new_state)

    # ------------------------------------------------------------------ api

    def poll(self) -> str:
        """Re-read the control file once. Returns the current (possibly new) state."""
        s = self._read_state()
        with self._lock:
            self._set_state(s)
            return self._last_state

    def gate(self) -> None:
        """
        Synchronous guard.

        Returns immediately when the state is ``running``. Polls the file
        every ``poll_interval_s`` seconds while ``paused``. Raises
        :class:`TakeoverRequested` as soon as the state is ``stopping``.
        """
        if not self.enabled:
            return
        while True:
            s = self.poll()
            if s == "running":
                return
            if s == "stopping":
                raise TakeoverRequested()
            time.sleep(self._poll_interval_s)

    async def gate_async(self) -> None:
        """
        Coroutine guard. Identical semantics to :meth:`gate`, but uses
        :func:`asyncio.sleep` so it does not block the event loop while
        paused. Useful from async LLM wrappers.
        """
        if not self.enabled:
            return
        while True:
            s = self.poll()
            if s == "running":
                return
            if s == "stopping":
                raise TakeoverRequested()
            await asyncio.sleep(self._poll_interval_s)


_GLOBAL_LOCK = threading.Lock()
_GLOBAL: AgentControl | None = None


def get_default() -> AgentControl:
    """Return the process-wide :class:`AgentControl` instance."""
    global _GLOBAL
    with _GLOBAL_LOCK:
        if _GLOBAL is None:
            _GLOBAL = AgentControl()
        return _GLOBAL
