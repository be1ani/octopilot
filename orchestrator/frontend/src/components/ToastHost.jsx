import { useEffect, useMemo, useRef, useState } from "react";
import "./ToastHost.css";

const TARGET_COST_USD = 0.2;
const TARGET_DURATION_MIN = 20;
const URGENT_ACK_KEY = "octopilot.urgentAck";

function fmtMoney(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return `$${n.toFixed(2)}`;
}

function fmtDuration(seconds, fallbackLabel) {
  const s = Number(seconds);
  if (!Number.isFinite(s)) return fallbackLabel || "—";
  const total = Math.max(0, Math.round(s));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const ss = total % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${ss}s`;
  return `${ss}s`;
}

function scoreTone({ costUsd, durationSeconds }) {
  const c = Number(costUsd);
  const d = Number(durationSeconds);
  const costRatio = Number.isFinite(c) ? c / TARGET_COST_USD : null;
  const timeRatio = Number.isFinite(d) ? d / 60 / TARGET_DURATION_MIN : null;

  if (costRatio === null && timeRatio === null) return "neutral";
  const ratio = Math.max(costRatio ?? 0, timeRatio ?? 0);
  if (ratio <= 1.0) return "good";
  if (ratio <= 1.5) return "warn";
  return "bad";
}

function Toast({ toast, onClose }) {
  return (
    <div className={`toast toast--${toast.tone}`} role="status" aria-live="polite">
      <div className="toast-top">
        <div className="toast-title">{toast.title}</div>
        <button type="button" className="toast-x" onClick={onClose} aria-label="Dismiss">
          ×
        </button>
      </div>
      <div className="toast-body">{toast.body}</div>
      {toast.href ? (
        <div className="toast-actions">
          <a className="toast-link" href={toast.href} target="_blank" rel="noreferrer">
            Open application ↗
          </a>
        </div>
      ) : null}
    </div>
  );
}

function UrgentToast({ alert, onAcknowledge }) {
  return (
    <div className={`toast toast--urgent ${alert?.acknowledged ? "toast--urgent-ack" : ""}`} role="status" aria-live="polite">
      <div className="toast-top">
        <div className="toast-title">{alert?.title || "Urgent"}</div>
        <button type="button" className="toast-x" onClick={onAcknowledge} aria-label="Acknowledge">
          Acknowledge
        </button>
      </div>
      <div className="toast-body">{alert?.body}</div>
      {alert?.hint ? <div className="toast-hint mono">{alert.hint}</div> : null}
    </div>
  );
}

/**
 * Polls latest application records and emits a toast for new "Finished" ones.
 * Expects backend to attach record.duration_seconds + record.cost_usd (best-effort).
 */
export function ToastHost({ pollMs = 2000, stats = null, settings = null, machines = [] }) {
  const [toasts, setToasts] = useState([]);
  const lastSeenIdRef = useRef(null);
  const [urgentAck, setUrgentAck] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem(URGENT_ACK_KEY) || "null");
    } catch {
      return null;
    }
  });
  const urgentAudioRef = useRef(null);
  const [urgentSoundBlocked, setUrgentSoundBlocked] = useState(false);

  const removeToast = (id) => setToasts((prev) => prev.filter((t) => t.id !== id));

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const res = await fetch("/api/applications?limit=1");
        const data = await res.json().catch(() => []);
        if (!alive) return;
        if (!res.ok) return;
        const rec = Array.isArray(data) ? data[0] : null;
        const rid = rec?.id || null;
        if (!rid || rid === lastSeenIdRef.current) return;
        lastSeenIdRef.current = rid;

        if ((rec?.status || "").toLowerCase() !== "finished") return;

        const durationSeconds = rec?.duration_seconds;
        const costUsd = rec?.cost_usd;
        const durationLabel = fmtDuration(durationSeconds, rec?.duration_label);
        const tone = scoreTone({ costUsd, durationSeconds });

        const toast = {
          id: rid,
          tone,
          title: "Application finished",
          body: (
            <span className="mono">
              {durationLabel} · {fmtMoney(costUsd)}
            </span>
          ),
          href: rec?.application_url || null,
        };

        setToasts((prev) => [toast, ...prev].slice(0, 4));
        window.setTimeout(() => removeToast(rid), 9000);
      } catch {
        /* ignore polling errors */
      }
    };

    tick();
    const id = window.setInterval(tick, pollMs);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [pollMs]);

  const urgent = useMemo(() => {
    const budgetExceeded = Boolean(stats?.budget_exceeded);
    if (!budgetExceeded) return null;

    const key = budgetExceeded
      ? `budget:${String(settings?.budget_alert_usd ?? stats?.budget_alert_usd ?? "")}`
      : "";
    const lastKey = typeof urgentAck?.key === "string" ? urgentAck.key : "";
    const acknowledged = lastKey === key && urgentAck?.ack === true;

    const title = "Budget exceeded";
    const body = (
      <span>
        Global cost is now <span className="mono">${Number(stats?.total_cost_usd || 0).toFixed(2)}</span> which is above the configured budget.
        Automatic and manual starts are blocked until you raise the budget.
      </span>
    );
    const hint = urgentSoundBlocked ? "Click anywhere in the page to enable the urgent sound." : "";
    return { key, acknowledged, title, body, hint };
  }, [machines, settings, stats, urgentAck, urgentSoundBlocked]);

  useEffect(() => {
    if (!urgent) {
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
    if (urgent.acknowledged) {
      a.pause();
      a.currentTime = 0;
      return;
    }

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
      if (!urgent) return;
      if (urgent.acknowledged) return;
      tryPlay();
    };
    window.addEventListener("pointerdown", onUserGesture, { once: true });
    return () => window.removeEventListener("pointerdown", onUserGesture);
  }, [urgent, urgentSoundBlocked]);

  const items = useMemo(() => toasts, [toasts]);

  if (!items.length && !urgent) return null;
  return (
    <div className="toast-host" aria-label="Notifications">
      {urgent ? (
        <UrgentToast
          alert={urgent}
          onAcknowledge={() => {
            const next = { key: urgent.key, ack: true, at: Date.now() };
            setUrgentAck(next);
            try {
              localStorage.setItem(URGENT_ACK_KEY, JSON.stringify(next));
            } catch {
              /* ignore */
            }
          }}
        />
      ) : null}
      {items.map((t) => (
        <Toast key={t.id} toast={t} onClose={() => removeToast(t.id)} />
      ))}
    </div>
  );
}

