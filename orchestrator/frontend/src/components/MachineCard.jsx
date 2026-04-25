import { useCallback, useEffect, useRef, useState } from "react";
import {
  FiCheckCircle,
  FiFileText,
  FiLock,
  FiMoreVertical,
  FiPause,
  FiPlay,
  FiRefreshCcw,
  FiSquare,
  FiTrash2,
  FiUnlock,
  FiX,
} from "react-icons/fi";
import { ReviewSubmitDialog } from "./ReviewSubmitDialog.jsx";
import "./MachineCard.css";

const HIDDEN_IMAGE_LABELS = new Set(["", "latest", "LATEST", "Latest"]);

/** Tty height under the desktop (desktop stays full view_height from API). */
const TERMINAL_HEIGHT = 220;
const BUDGET_ACK_PREFIX = "okto.budgetAck.";
/** Auto-relock the desktop after this many seconds of no observed activity. */
const AUTO_LOCK_IDLE_S = 10;

function injectDarkScrollbarsIntoIframe(iframeEl) {
  if (!iframeEl) return;
  try {
    const doc = iframeEl.contentDocument;
    if (!doc) return;

    const styleId = "okto-dark-scrollbars";
    if (doc.getElementById(styleId)) return;

    const style = doc.createElement("style");
    style.id = styleId;
    style.textContent = `
      :root { color-scheme: dark; }
      html, body {
        scrollbar-color: rgba(148, 163, 184, 0.32) transparent;
        scrollbar-width: thin;
        background: transparent;
      }
      *::-webkit-scrollbar { width: 10px; height: 10px; }
      *::-webkit-scrollbar-track { background: transparent; }
      *::-webkit-scrollbar-thumb {
        background-color: rgba(148, 163, 184, 0.22);
        border-radius: 999px;
        border: 3px solid transparent;
        background-clip: content-box;
      }
      *::-webkit-scrollbar-thumb:hover { background-color: rgba(148, 163, 184, 0.34); }
      *::-webkit-scrollbar-corner { background: transparent; }
    `;

    doc.head?.appendChild(style);
  } catch {
    // Cross-origin iframe; can't style its internal scrollbars.
  }
}

function StatusDot({ status }) {
  const map = {
    running: "ok",
    starting: "pending",
    stopped: "off",
    error: "err",
  };
  return <span className={`status-dot ${map[status] || "off"}`} title={status} />;
}

function fmtCompact(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return "0";
  const abs = Math.abs(v);
  const units = [
    { k: 1e12, s: "T" },
    { k: 1e9, s: "B" },
    { k: 1e6, s: "M" },
    { k: 1e3, s: "K" },
  ];
  for (const u of units) {
    if (abs >= u.k) {
      const x = v / u.k;
      const d = abs >= u.k * 100 ? 0 : 1; // fewer decimals when large
      return `${x.toFixed(d)}${u.s}`;
    }
  }
  return String(Math.round(v));
}

export function MachineCard({
  machine,
  onStop,
  onRestart,
  onAgentPause,
  onAgentTakeover,
  onRemove,
  settings,
  onUpdateSettings,
}) {
  const {
    id,
    job_url: jobUrl,
    status,
    image_label: imageLabel,
    llm_model: llmModel,
    needs_human: needsHuman,
    needs_human_reason: needsHumanReason,
    uptime_label: uptimeLabel,
    cost_usd: costUsd,
    desktop_url: desktopUrl,
    terminal_url: terminalUrl,
    desktop_ready: desktopReady,
    terminal_ready: terminalReady,
    view_width: vw,
    view_height: vh,
    error,
    llm_tokens: llmTokens,
    container_id: containerId,
    agent_paused: rawAgentPaused,
    agent_state: rawAgentState,
  } = machine;
  // `agent_paused` is "what we asked for"; `agent_state` is "what the agent
  // is actually doing" (reported via telemetry). Trust the observed state
  // when present, and fall back to the intended state otherwise.
  const observedState = typeof rawAgentState === "string" ? rawAgentState : null;
  const agentPaused =
    observedState === "paused"
      ? true
      : observedState === "running"
        ? false
        : Boolean(rawAgentPaused);
  const takeoverInProgress = observedState === "stopping";
  const budgetExceeded = Boolean(needsHuman) && String(needsHumanReason || "").includes("budget_per_machine_exceeded");

  const budgetAckKey = `${BUDGET_ACK_PREFIX}${id}`;
  const [budgetAck, setBudgetAck] = useState(() => {
    try {
      const v = localStorage.getItem(budgetAckKey);
      return v === "1";
    } catch {
      return false;
    }
  });

  // If a new "budget exceeded" event arrives later, re-arm the overlay/highlight.
  const prevBudgetExceededRef = useRef(false);
  useEffect(() => {
    const prev = prevBudgetExceededRef.current;
    prevBudgetExceededRef.current = budgetExceeded;
    if (!prev && budgetExceeded) {
      // Fresh budget-exceeded incident; make sure we show it even if the prior one was acknowledged.
      setBudgetAck(false);
    }
  }, [budgetExceeded]);

  useEffect(() => {
    try {
      localStorage.setItem(budgetAckKey, budgetAck ? "1" : "0");
    } catch {
      /* ignore */
    }
  }, [budgetAckKey, budgetAck]);

  const urgentAudioRef = useRef(null);
  const [urgentSoundBlocked, setUrgentSoundBlocked] = useState(false);

  useEffect(() => {
    if (!budgetExceeded || budgetAck) {
      setUrgentSoundBlocked(false);
      if (urgentAudioRef.current) {
        urgentAudioRef.current.pause();
        urgentAudioRef.current.currentTime = 0;
      }
      return;
    }

    if (!urgentAudioRef.current) {
      urgentAudioRef.current = new Audio("/urgent.wav");
      urgentAudioRef.current.loop = true;
      urgentAudioRef.current.preload = "auto";
      urgentAudioRef.current.volume = 1.0;
    }
    const a = urgentAudioRef.current;
    const tryPlay = async () => {
      try {
        await a.play();
        setUrgentSoundBlocked(false);
      } catch {
        setUrgentSoundBlocked(true);
      }
    };
    tryPlay();

    if (!urgentSoundBlocked) return;
    const onUserGesture = () => {
      if (!budgetExceeded || budgetAck) return;
      tryPlay();
    };
    window.addEventListener("pointerdown", onUserGesture, { once: true });
    return () => window.removeEventListener("pointerdown", onUserGesture);
  }, [budgetExceeded, budgetAck, urgentSoundBlocked]);

  const showFrames = status === "running" && desktopUrl && terminalUrl && desktopReady && terminalReady;

  const canStop = status !== "stopped" && status !== "error";
  const canRestart = status !== "starting";
  const canPauseAgent = status === "running" && Boolean(containerId);

  const lockKey = `okto.machineLocked.${id}`;
  const [locked, setLocked] = useState(() => {
    try {
      const v = localStorage.getItem(lockKey);
      if (v === null) return true; // locked by default
      return v === "1";
    } catch {
      return true;
    }
  });

  useEffect(() => {
    try {
      localStorage.setItem(lockKey, locked ? "1" : "0");
    } catch {
      /* ignore */
    }
  }, [lockKey, locked]);

  const canToggleLock = showFrames;

  // Refs + state for the inactivity auto-lock. The desktop iframe is
  // cross-origin, so we cannot observe click/scroll events inside it from the
  // parent page. As a reliable proxy, we combine:
  //   * real DOM events on the card wrapper (pointermove/down, wheel, keydown),
  //   * focusin on the iframe element (click inside the iframe focuses it),
  //   * a 250 ms poll of `document.activeElement === iframeEl` so sustained
  //     scrolling/typing inside the iframe keeps the countdown reset.
  // Any of these signals resets `lastActivity`; after `AUTO_LOCK_IDLE_S`
  // seconds of silence we flip `locked` back to true.
  const cardRef = useRef(null);
  const desktopIframeRef = useRef(null);
  const [idleSecs, setIdleSecs] = useState(AUTO_LOCK_IDLE_S);

  useEffect(() => {
    if (!showFrames || locked) {
      setIdleSecs(AUTO_LOCK_IDLE_S);
      return undefined;
    }
    let lastActivity = Date.now();
    const resetActivity = () => {
      lastActivity = Date.now();
    };

    const cardEl = cardRef.current;
    const passive = { passive: true };
    if (cardEl) {
      cardEl.addEventListener("pointermove", resetActivity, passive);
      cardEl.addEventListener("pointerdown", resetActivity, passive);
      cardEl.addEventListener("wheel", resetActivity, passive);
      cardEl.addEventListener("keydown", resetActivity);
    }
    const onFocusIn = (e) => {
      if (e.target === desktopIframeRef.current) resetActivity();
    };
    window.addEventListener("focusin", onFocusIn);

    const interval = setInterval(() => {
      if (document.activeElement === desktopIframeRef.current) {
        lastActivity = Date.now();
      }
      const idleMs = Date.now() - lastActivity;
      const remaining = Math.max(0, AUTO_LOCK_IDLE_S - Math.floor(idleMs / 1000));
      setIdleSecs(remaining);
      if (remaining <= 0) {
        setLocked(true);
      }
    }, 250);

    return () => {
      clearInterval(interval);
      if (cardEl) {
        cardEl.removeEventListener("pointermove", resetActivity);
        cardEl.removeEventListener("pointerdown", resetActivity);
        cardEl.removeEventListener("wheel", resetActivity);
        cardEl.removeEventListener("keydown", resetActivity);
      }
      window.removeEventListener("focusin", onFocusIn);
    };
  }, [showFrames, locked]);

  const [logsOpen, setLogsOpen] = useState(false);
  const [logsText, setLogsText] = useState("");
  const [logsLoading, setLogsLoading] = useState(false);
  const [logsErr, setLogsErr] = useState(null);
  const [logsTruncated, setLogsTruncated] = useState(false);
  const [logsTotalBytes, setLogsTotalBytes] = useState(0);
  const [pauseBusy, setPauseBusy] = useState(false);
  const [budgetBusy, setBudgetBusy] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef(null);
  const menuTriggerRef = useRef(null);

  useEffect(() => {
    if (!menuOpen) return undefined;
    const onDown = (e) => {
      const t = e.target;
      if (menuRef.current && menuRef.current.contains(t)) return;
      if (menuTriggerRef.current && menuTriggerRef.current.contains(t)) return;
      setMenuOpen(false);
    };
    const onKey = (e) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  const closeMenuAnd = (fn) => (...args) => {
    setMenuOpen(false);
    return fn?.(...args);
  };

  const showImagePill = Boolean(imageLabel) && !HIDDEN_IMAGE_LABELS.has(String(imageLabel).trim());

  // --- Human-review / submit dialog ------------------------------------------
  // As soon as the agent reports a final result for this machine (persisted
  // via the orchestrator's application-result endpoint), surface a "Submit"
  // button so a human can confirm / edit the final status, description and
  // filled fields — even while the container is still running. We used to
  // only poll when the machine had stopped/errored, which meant the review
  // button would not appear until the container was torn down.
  const [latestApp, setLatestApp] = useState(null);
  const [reviewOpen, setReviewOpen] = useState(false);

  useEffect(() => {
    if (status === "starting") return undefined;
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`/api/machines/${id}/latest-application`);
        const data = await res.json().catch(() => null);
        if (!cancelled && res.ok) setLatestApp(data || null);
      } catch {
        /* ignore; a later refresh will retry */
      }
    };
    load();
    const t = setInterval(load, 5000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [status, id]);

  const submitDisabled = !latestApp;
  const submitTitle = latestApp
    ? (latestApp.reviewed ? "View the recorded application" : "Review & submit the recorded application")
    : "No application recorded yet for this machine";

  const loadLogs = useCallback(async () => {
    setLogsLoading(true);
    setLogsErr(null);
    try {
      const res = await fetch(`/api/machines/${id}/terminal-logs`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.error || res.statusText || "Failed to load logs");
      }
      setLogsText(typeof data.text === "string" ? data.text : "");
      setLogsTruncated(Boolean(data.truncated));
      setLogsTotalBytes(Number(data.total_bytes) || 0);
    } catch (e) {
      setLogsErr(e?.message || "Failed to load logs");
    } finally {
      setLogsLoading(false);
    }
  }, [id]);

  useEffect(() => {
    if (!logsOpen) return undefined;
    loadLogs();
    if (status !== "running") return undefined;
    const t = setInterval(loadLogs, 4000);
    return () => clearInterval(t);
  }, [logsOpen, status, loadLogs]);

  useEffect(() => {
    if (!logsOpen) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") setLogsOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [logsOpen]);

  const showBudgetOverlay = budgetExceeded && !budgetAck;
  const showAttention = needsHuman && !(budgetExceeded && budgetAck);

  return (
    <article
      ref={cardRef}
      className={`machine-card ${showAttention ? "machine-card--attention" : ""}`}
    >
      <header className="machine-card-head">
        <div className="machine-head-row">
          <div className="machine-head-left">
            <StatusDot status={status} />
            <span className="machine-status-pill">{status}</span>
            {showImagePill ? (
              <span className="machine-version-pill" title="Agent image version">
                {imageLabel}
              </span>
            ) : null}
            {llmModel ? (
              <span className="machine-version-pill machine-llm-pill" title="LLM model">
                {llmModel}
              </span>
            ) : null}
            {needsHuman ? (
              <span
                className="attention-pill"
                title={needsHumanReason ? `Needs human: ${needsHumanReason}` : "Needs human intervention"}
              >
                HUMAN
              </span>
            ) : null}
            {agentPaused ? (
              <span
                className="paused-pill"
                title="Agent is paused — no further LLM calls will be issued until resumed."
              >
                <FiPause aria-hidden />
                <span>PAUSED</span>
              </span>
            ) : null}
          </div>
          <div className="machine-head-metrics mono">
            <span title="Uptime">{uptimeLabel}</span>
            <span className="mh-sep" aria-hidden>
              ·
            </span>
            <span className="cost-pill" title="Cost (est.)">
              ${Number(costUsd).toFixed(2)}
            </span>
            <span className="mh-sep" aria-hidden>
              ·
            </span>
            <span title="LLM tokens (this machine)">{fmtCompact(llmTokens)} tok</span>
          </div>
          <div className="machine-menu-wrap">
            <button
              ref={menuTriggerRef}
              type="button"
              className="machine-menu-trigger"
              onClick={() => setMenuOpen((v) => !v)}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              aria-label="Machine controls"
              title="Machine controls"
            >
              <FiMoreVertical />
            </button>
            {menuOpen ? (
              <div ref={menuRef} className="machine-menu" role="menu">
                <button
                  type="button"
                  role="menuitem"
                  className="machine-menu-item machine-menu-item--danger"
                  onClick={closeMenuAnd(() => onStop?.(id))}
                  disabled={!canStop}
                >
                  <FiSquare aria-hidden />
                  <span>Stop</span>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="machine-menu-item"
                  onClick={closeMenuAnd(() => onRestart?.(id))}
                  disabled={!canRestart}
                >
                  <FiRefreshCcw aria-hidden />
                  <span>Restart</span>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className={`machine-menu-item${agentPaused ? " machine-menu-item--paused" : ""}`}
                  onClick={async () => {
                    setMenuOpen(false);
                    if (!onAgentPause || !canPauseAgent) return;
                    setPauseBusy(true);
                    try {
                      await onAgentPause(id, !agentPaused);
                    } finally {
                      setPauseBusy(false);
                    }
                  }}
                  disabled={!canPauseAgent || pauseBusy || takeoverInProgress}
                  aria-pressed={Boolean(agentPaused)}
                  title={
                    takeoverInProgress
                      ? "Takeover in progress — restart the machine to resume the agent"
                      : undefined
                  }
                >
                  {agentPaused ? <FiPlay aria-hidden /> : <FiPause aria-hidden />}
                  <span>
                    {takeoverInProgress
                      ? "Stopping…"
                      : agentPaused
                        ? "Resume agent"
                        : "Pause agent"}
                  </span>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="machine-menu-item"
                  onClick={closeMenuAnd(() => setLocked((v) => !v))}
                  disabled={!canToggleLock}
                  aria-pressed={!locked}
                >
                  {locked ? <FiLock aria-hidden /> : <FiUnlock aria-hidden />}
                  <span>{locked ? "Unlock desktop" : "Lock desktop"}</span>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className="machine-menu-item"
                  onClick={closeMenuAnd(() => setLogsOpen(true))}
                >
                  <FiFileText aria-hidden />
                  <span>Terminal logs</span>
                </button>
                <button
                  type="button"
                  role="menuitem"
                  className={`machine-menu-item${latestApp && !latestApp.reviewed ? " machine-menu-item--submit" : ""}`}
                  onClick={closeMenuAnd(() => setReviewOpen(true))}
                  disabled={submitDisabled}
                  title={submitTitle}
                >
                  <FiCheckCircle aria-hidden />
                  <span>
                    {latestApp ? (latestApp.reviewed ? "View application" : "Review & submit") : "Submit"}
                  </span>
                </button>
                <div className="machine-menu-sep" role="separator" />
                <button
                  type="button"
                  role="menuitem"
                  className="machine-menu-item machine-menu-item--danger2"
                  onClick={() => {
                    setMenuOpen(false);
                    if (confirm("Remove this machine from the grid? This will also remove the container.")) {
                      onRemove?.(id);
                    }
                  }}
                >
                  <FiTrash2 aria-hidden />
                  <span>Remove machine</span>
                </button>
              </div>
            ) : null}
          </div>
        </div>
        <a
          className="machine-url-bar"
          href={jobUrl}
          target="_blank"
          rel="noreferrer"
          title={jobUrl}
        >
          {jobUrl}
        </a>
      </header>

      {error && status === "error" && <div className="machine-error">{error}</div>}

      <div className="machine-card-body">
        <div className="desktop-stack">
          <div className="desktop-frame" style={{ width: vw }}>
            {desktopUrl ? (
              <a
                className="desktop-open"
                href={desktopUrl}
                target="_blank"
                rel="noreferrer"
              >
                VNC ↗
              </a>
            ) : null}
            <div
              className="iframe-shell desktop-shell"
              style={{ width: vw, height: vh }}
            >
            {showFrames ? (
              <iframe
                ref={desktopIframeRef}
                title={`desktop-${id}`}
                src={desktopUrl}
                width={vw}
                height={vh}
                className="embed"
                onLoad={(e) => injectDarkScrollbarsIntoIframe(e.currentTarget)}
              />
            ) : (
              <div className="iframe-placeholder">
                {status === "stopped"
                  ? "Session ended"
                  : status === "starting" || desktopReady === false
                    ? "Booting desktop…"
                    : "Desktop unavailable"}
              </div>
            )}
            </div>

            {showFrames ? (
              <div
                className={`desktop-lock-overlay ${
                  locked ? "desktop-lock-overlay--locked" : "desktop-lock-overlay--unlocked"
                }`}
                role="presentation"
              >
                <div
                  className={`desktop-lock-chip ${
                    locked ? "desktop-lock-chip--locked" : "desktop-lock-chip--unlocked"
                  }`}
                >
                  {locked ? <FiLock aria-hidden /> : <FiUnlock aria-hidden />}
                  <span className="desktop-lock-label">
                    {locked ? "Locked" : "Unlocked"}
                    {!locked && idleSecs < AUTO_LOCK_IDLE_S ? (
                      <span className="desktop-lock-countdown mono">
                        {" · auto-lock in "}
                        <strong>{idleSecs}s</strong>
                      </span>
                    ) : null}
                  </span>
                  <button
                    type="button"
                    className="desktop-lock-cta"
                    onClick={() => setLocked((v) => !v)}
                    title={locked ? "Unlock the desktop to take control" : "Lock the desktop now"}
                  >
                    {locked ? "Unlock" : "Lock"}
                  </button>
                </div>
              </div>
            ) : null}
          </div>
          <div
            className="iframe-shell terminal-shell"
            style={{ width: vw, height: TERMINAL_HEIGHT }}
          >
            {showFrames ? (
              <iframe
                title={`terminal-${id}`}
                src={terminalUrl}
                width={vw}
                height={TERMINAL_HEIGHT}
                className="embed"
                onLoad={(e) => injectDarkScrollbarsIntoIframe(e.currentTarget)}
              />
            ) : (
              <div className="iframe-placeholder">
                {status === "stopped"
                  ? "—"
                  : status === "starting" || terminalReady === false
                    ? "Booting terminal…"
                    : "Terminal unavailable"}
              </div>
            )}
          </div>

        </div>
      </div>

      {showBudgetOverlay ? (
        <div className={`machine-budget-overlay ${budgetAck ? "machine-budget-overlay--ack" : ""}`}>
          <div className="machine-budget-overlay-main">
            <div className="machine-budget-title">Budget exceeded for this machine</div>
            <div className="machine-budget-sub mono">
              LLM cost limit hit. Agent is paused and requires manual intervention.
              {urgentSoundBlocked ? " Click anywhere to enable sound." : ""}
            </div>
          </div>
          <div className="machine-budget-actions">
            <button
              type="button"
              className="btn ghost btn--small"
              onClick={async () => {
                // Ask the agent to stop gracefully (cooperative takeover) and
                // unlock the desktop so the user can drive it. We prefer the
                // dedicated /takeover endpoint over /agent-pause because it
                // exits the agent instead of just blocking it, which means
                // no further LLM calls will ever be issued for this run.
                setBudgetAck(true);
                setLocked(false);
                if (!canPauseAgent) return;
                setPauseBusy(true);
                try {
                  if (onAgentTakeover) {
                    await onAgentTakeover(id);
                  } else if (onAgentPause) {
                    await onAgentPause(id, true);
                  }
                } finally {
                  setPauseBusy(false);
                }
              }}
              disabled={!canPauseAgent || pauseBusy}
              title="Stop the agent and unlock the desktop so you can take over"
            >
              Human take over
            </button>
            <button
              type="button"
              className="btn primary btn--small"
              onClick={async () => {
                if (!onUpdateSettings) return;
                setBudgetAck(true);
                setBudgetBusy(true);
                try {
                  const cur = Number(settings?.max_budget_per_machine_usd || 0) || 0;
                  const next = Math.max(0, cur + 0.5);
                  await onUpdateSettings({ max_budget_per_machine_usd: next });
                } finally {
                  setBudgetBusy(false);
                }
              }}
              disabled={!onUpdateSettings || budgetBusy}
              title="Increase max budget per machine by $0.50"
            >
              {budgetBusy ? "Increasing…" : "Increase budget +$0.50"}
            </button>
          </div>
        </div>
      ) : null}

      {logsOpen ? (
        <div
          className="machine-log-backdrop"
          role="presentation"
          onClick={(e) => {
            if (e.target === e.currentTarget) setLogsOpen(false);
          }}
        >
          <div
            className="machine-log-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby={`machine-log-title-${id}`}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="machine-log-modal-head">
              <h2 id={`machine-log-title-${id}`} className="machine-log-modal-title">
                Terminal logs
              </h2>
              <div className="machine-log-modal-actions">
                <span className="machine-log-meta mono">
                  {logsTotalBytes > 0 ? `${logsTotalBytes.toLocaleString()} bytes` : "—"}
                  {logsTruncated ? " · tail only" : ""}
                </span>
                <button
                  type="button"
                  className="machine-log-refresh"
                  onClick={() => loadLogs()}
                  disabled={logsLoading}
                >
                  Refresh
                </button>
                <button
                  type="button"
                  className="btn-sandwich-btn btn-sandwich-btn--icon"
                  onClick={() => setLogsOpen(false)}
                  title="Close"
                  aria-label="Close"
                >
                  <FiX />
                </button>
              </div>
            </div>
            <div className="machine-log-modal-body">
              {logsErr ? <div className="machine-log-error">{logsErr}</div> : null}
              {logsLoading && !logsText ? <div className="machine-log-loading">Loading…</div> : null}
              <pre className="machine-log-pre">{logsText || (!logsLoading && !logsErr ? "(empty)" : "")}</pre>
            </div>
          </div>
        </div>
      ) : null}

      {reviewOpen && latestApp ? (
        <ReviewSubmitDialog
          record={latestApp}
          onClose={() => setReviewOpen(false)}
          onSubmitted={(updated) => setLatestApp(updated)}
          onRemoveMachine={() => onRemove?.(id)}
        />
      ) : null}
    </article>
  );
}
