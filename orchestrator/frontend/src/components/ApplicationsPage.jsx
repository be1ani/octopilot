import { useEffect, useMemo, useState } from "react";
import { ScreenshotCarousel } from "./ScreenshotCarousel.jsx";
import { formatRelativeTime, urlHostname } from "../format.js";
import "./ApplicationsPage.css";

const APP_STATUSES = ["In progress", "Finished", "Submitted", "Not found", "Failed"];

/** Counted toward analytics (successful outcomes only). */
const POSITIVE_APP_STATUSES = new Set(["Finished", "Submitted"]);

function localDateKey(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function isPositiveApplicationStatus(status) {
  const s = (status || "").trim() || "In progress";
  return POSITIVE_APP_STATUSES.has(s);
}

/** Rolling calendar days including today (length = numDays). */
function dateKeysEndingToday(now, numDays) {
  const end = new Date(now);
  end.setHours(0, 0, 0, 0);
  const keys = [];
  for (let i = numDays - 1; i >= 0; i--) {
    const d = new Date(end);
    d.setDate(d.getDate() - i);
    keys.push(localDateKey(d));
  }
  return keys;
}

function buildPositiveApplicationStats(rows, now) {
  const positive = rows.filter((r) => isPositiveApplicationStatus(r?.status));
  const todayKey = localDateKey(now);

  const weekKeys = new Set(dateKeysEndingToday(now, 7));
  const dailyKeys = dateKeysEndingToday(now, 30);
  const dailyMap = Object.fromEntries(dailyKeys.map((k) => [k, 0]));

  let today = 0;
  let week = 0;

  for (const r of positive) {
    const iso = r?.created_at_iso;
    if (!iso) continue;
    const t = new Date(iso);
    if (Number.isNaN(t.getTime())) continue;
    const k = localDateKey(t);
    if (k === todayKey) today += 1;
    if (weekKeys.has(k)) week += 1;
    if (Object.prototype.hasOwnProperty.call(dailyMap, k)) dailyMap[k] += 1;
  }

  const dailyCounts = dailyKeys.map((k) => dailyMap[k]);
  return {
    total: positive.length,
    today,
    week,
    dailyLabels: dailyKeys,
    dailyCounts,
  };
}

function DailyApplicationsChart({ labels, counts }) {
  const n = labels.length;
  if (!n) return null;

  const W = 640;
  const H = 148;
  const pad = { t: 14, r: 10, b: 28, l: 36 };
  const innerW = W - pad.l - pad.r;
  const innerH = H - pad.t - pad.b;
  const maxV = Math.max(1, ...counts);

  const xAt = (i) => {
    if (n <= 1) return pad.l + innerW / 2;
    return pad.l + (innerW * i) / (n - 1);
  };
  const yAt = (v) => pad.t + innerH * (1 - v / maxV);

  const linePts = counts.map((v, i) => `${xAt(i)},${yAt(v)}`).join(" ");
  const firstX = xAt(0);
  const lastX = xAt(n - 1);
  const bottomY = pad.t + innerH;
  const areaD = `M ${firstX} ${bottomY} L ${counts.map((v, i) => `${xAt(i)} ${yAt(v)}`).join(" L ")} L ${lastX} ${bottomY} Z`;

  const tickIdx = Array.from(new Set([0, Math.floor((n - 1) / 2), n - 1])).sort((a, b) => a - b);

  return (
    <div className="apps-chart-wrap" role="img" aria-label="Daily count of successful applications over the last 30 days">
      <svg className="apps-chart-svg" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet">
        <defs>
          <linearGradient id="appsChartFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgba(56, 189, 248, 0.35)" />
            <stop offset="100%" stopColor="rgba(56, 189, 248, 0.02)" />
          </linearGradient>
        </defs>
        <rect x={pad.l} y={pad.t} width={innerW} height={innerH} fill="rgba(2, 6, 23, 0.25)" rx={6} />
        {[0, 0.5, 1].map((frac) => {
          const y = pad.t + innerH * (1 - frac);
          return (
            <line
              key={frac}
              x1={pad.l}
              y1={y}
              x2={pad.l + innerW}
              y2={y}
              stroke="rgba(148, 163, 184, 0.18)"
              strokeWidth={1}
              strokeDasharray={frac === 1 ? "0" : "4 4"}
            />
          );
        })}
        <path d={areaD} fill="url(#appsChartFill)" />
        <polyline fill="none" stroke="#38bdf8" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" points={linePts} />
        {counts.map((v, i) => (
          <circle key={`pt-${i}`} cx={xAt(i)} cy={yAt(v)} r={3.5} fill="#0ea5e9" stroke="rgba(15, 23, 42, 0.9)" strokeWidth={1} />
        ))}
        <text x={pad.l} y={12} fill="rgba(148, 163, 184, 0.95)" fontSize={11} fontWeight={700}>
          {maxV}
        </text>
        {tickIdx.map((i) => {
          const short = labels[i]?.slice(5) || "";
          return (
            <text
              key={labels[i]}
              x={xAt(i)}
              y={H - 6}
              textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}
              fill="rgba(148, 163, 184, 0.88)"
              fontSize={10}
            >
              {short}
            </text>
          );
        })}
      </svg>
    </div>
  );
}

function StatusPill({ status }) {
  const cls =
    status === "Finished"
      ? "ok"
      : status === "Submitted"
        ? "submitted"
        : status === "Not found"
        ? "nf"
        : status === "Failed"
          ? "fail"
          : "unk";
  return <span className={`app-status app-status--${cls}`}>{status || "Unknown"}</span>;
}

export function ApplicationsPage() {
  const [apps, setApps] = useState([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const [expandedId, setExpandedId] = useState(null);
  const [statusSavingId, setStatusSavingId] = useState(null);
  // Re-render once a minute so "3 min ago" stays fresh without manual refresh.
  const [tick, setTick] = useState(0);

  const refresh = async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/applications?limit=2000");
      const data = await res.json().catch(() => []);
      if (!res.ok) throw new Error(data?.error || res.statusText || "Request failed");
      setApps(Array.isArray(data) ? data : []);
      setErr(null);
    } catch (e) {
      setErr(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  };

  async function patchApplicationStatus(appId, nextStatus, prevStatus) {
    if (!appId || !nextStatus || nextStatus === prevStatus) return;
    setStatusSavingId(appId);
    setErr(null);
    try {
      const res = await fetch(`/api/applications/${encodeURIComponent(appId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: nextStatus }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.error || res.statusText || "Request failed");
      setApps((prev) => prev.map((x) => (x.id === appId ? { ...x, ...data } : x)));
    } catch (e) {
      setErr(e?.message || String(e));
    } finally {
      setStatusSavingId(null);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 60_000);
    return () => clearInterval(id);
  }, []);

  const rows = useMemo(() => (Array.isArray(apps) ? apps : []), [apps]);
  const now = useMemo(() => new Date(), [tick]);
  const stats = useMemo(() => buildPositiveApplicationStats(rows, now), [rows, now]);

  return (
    <div className="apps-page">
      <div className="apps-head">
        <div>
          <div className="apps-title">Past applications</div>
          <div className="apps-sub muted">Stored on disk by the orchestrator backend.</div>
        </div>
        <div className="apps-actions">
          <button type="button" className="btn ghost" onClick={refresh} disabled={loading}>
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      {err ? <div className="banner danger">{err}</div> : null}

      {!loading || rows.length ? (
        <div className="apps-stats">
          <div className="apps-stats-head">
            <div className="apps-stats-title">Successful applications</div>
            <div className="apps-stats-sub muted">Finished or Submitted only · times are local</div>
          </div>
          <div className="apps-stat-cards">
            <div className="apps-stat-card">
              <div className="apps-stat-label">Today</div>
              <div className="apps-stat-value">{stats.today}</div>
            </div>
            <div className="apps-stat-card">
              <div className="apps-stat-label">Last 7 days</div>
              <div className="apps-stat-value">{stats.week}</div>
            </div>
            <div className="apps-stat-card">
              <div className="apps-stat-label">Total</div>
              <div className="apps-stat-value">{stats.total}</div>
            </div>
          </div>
          <p className="apps-stats-footnote muted">Based on the most recent records returned from the server (up to 2000).</p>
          <DailyApplicationsChart labels={stats.dailyLabels} counts={stats.dailyCounts} />
          <div className="apps-chart-caption muted">Daily volume · last 30 days</div>
        </div>
      ) : null}

      {loading && !rows.length ? (
        <div className="apps-empty muted">Loading…</div>
      ) : !rows.length ? (
        <div className="apps-empty muted">No past applications recorded yet.</div>
      ) : (
        <div className="apps-table">
          <div className="apps-row apps-row--head">
            <div>When</div>
            <div>Status</div>
            <div>Job</div>
            <div>Details</div>
          </div>
          {rows.map((r) => {
            const stableAppId = r?.id || "";
            const rowKey = stableAppId || `${r?.machine_id || "m"}:${r?.created_at_iso || ""}:${r?.application_url || ""}`;
            const open = expandedId === rowKey;
            const url = r?.application_url || "";
            const desc = r?.description || "";
            const fields = r?.fields && typeof r.fields === "object" ? r.fields : {};
            const createdIso = r?.created_at_iso || "";
            const mid = r?.machine_id || "";
            const shots = Array.isArray(r?.screenshots) ? r.screenshots : [];
            const hasFields = Object.keys(fields).length > 0;
            const title = (r?.job_title || "").trim();
            const company = (r?.job_company || "").trim();
            const city = (r?.job_city || "").trim();
            const host = urlHostname(url);
            const relWhen = createdIso ? formatRelativeTime(createdIso, now) : "";
            const fallbackHeadline = title || company ? "" : url || "—";
            const statusCur = (r?.status || "").trim() || "In progress";
            return (
              <div key={rowKey} className={`apps-row ${open ? "apps-row--open" : ""}`}>
                <div className="apps-when" title={createdIso || ""}>{relWhen || "—"}</div>
                <div className="apps-status-cell">
                  {stableAppId ? (
                    <select
                      className="apps-status-select"
                      value={statusCur}
                      disabled={statusSavingId === stableAppId}
                      aria-label="Application status"
                      onChange={(e) => patchApplicationStatus(stableAppId, e.target.value, r?.status)}
                    >
                      {!APP_STATUSES.includes(statusCur) ? (
                        <option value={statusCur}>{statusCur}</option>
                      ) : null}
                      {APP_STATUSES.map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <StatusPill status={r?.status} />
                  )}
                </div>
                <div className="apps-job-cell">
                  {title || company ? (
                    <>
                      <div className="apps-job-title" title={title || company}>
                        {title || company}
                      </div>
                      <div className="apps-job-sub">
                        {title && company ? company : ""}
                        {title && company && city ? " · " : ""}
                        {city}
                        {(title || company) && host ? " · " : ""}
                        {host ? (
                          url ? (
                            <a
                              href={url}
                              target="_blank"
                              rel="noreferrer"
                              className="apps-host-link"
                              title={url}
                            >
                              {host}
                            </a>
                          ) : (
                            <span>{host}</span>
                          )
                        ) : null}
                      </div>
                    </>
                  ) : url ? (
                    <a href={url} target="_blank" rel="noreferrer" title={url} className="apps-url">
                      {fallbackHeadline}
                    </a>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </div>
                <div>
                  <button type="button" className="btn ghost btn--small" onClick={() => setExpandedId(open ? null : rowKey)}>
                    {open ? "Hide" : "Show"}
                  </button>
                </div>
                {open ? (
                  <div className="apps-detail">
                    {(r?.duration_seconds != null || r?.cost_usd != null || r?.llm_model) && (
                      <div className="apps-metrics">
                        <div className="apps-detail-label">Metrics</div>
                        <div className="mono apps-metrics-row">
                          <span title="Duration">{r?.duration_label || `${Math.round(Number(r.duration_seconds) || 0)}s`}</span>
                          <span className="mh-sep" aria-hidden>
                            ·
                          </span>
                          <span title={r?.llm_cost_usd != null ? "LLM cost (token-based estimate)" : "Cost (est.)"}>
                            {r?.llm_cost_usd != null
                              ? `$${Number(r.llm_cost_usd).toFixed(4)}`
                              : r?.cost_usd != null
                                ? `$${Number(r.cost_usd).toFixed(2)}`
                                : "—"}
                          </span>
                          {r?.llm_tokens != null ? (
                            <>
                              <span className="mh-sep" aria-hidden>
                                ·
                              </span>
                              <span title="LLM tokens (total)">{Number(r.llm_tokens).toLocaleString()} tok</span>
                            </>
                          ) : null}
                          {r?.llm_model ? (
                            <>
                              <span className="mh-sep" aria-hidden>
                                ·
                              </span>
                              <span title="LLM model used for this run" className="apps-model">{r.llm_model}</span>
                            </>
                          ) : null}
                        </div>
                      </div>
                    )}
                    <div className="apps-meta">
                      <div className="apps-detail-label">Context</div>
                      <div className="apps-meta-grid mono">
                        <div className="apps-meta-k">Machine</div>
                        <div className="apps-meta-v">{mid || "—"}</div>
                        {r?.profile_id ? (
                          <>
                            <div className="apps-meta-k">Profile</div>
                            <div className="apps-meta-v">{r.profile_id}</div>
                          </>
                        ) : null}
                        {url ? (
                          <>
                            <div className="apps-meta-k">URL</div>
                            <div className="apps-meta-v apps-meta-url">
                              <a href={url} target="_blank" rel="noreferrer" title={url}>{url}</a>
                            </div>
                          </>
                        ) : null}
                        <div className="apps-meta-k">When</div>
                        <div className="apps-meta-v">{createdIso || "—"}</div>
                      </div>
                    </div>
                    {desc ? (
                      <div className="apps-desc">
                        <div className="apps-detail-label">Result</div>
                        <pre className="apps-pre">{desc}</pre>
                      </div>
                    ) : null}
                    <div className="apps-fields">
                      <div className="apps-detail-label">
                        Filled fields {hasFields ? `(${Object.keys(fields).length})` : ""}
                      </div>
                      {hasFields ? (
                        <pre className="apps-pre">{JSON.stringify(fields, null, 2)}</pre>
                      ) : (
                        <div className="muted" style={{ fontSize: "0.8rem" }}>
                          No form fields were recorded for this application.
                        </div>
                      )}
                    </div>
                    <div className="apps-shots">
                      <div className="apps-detail-label">
                        Screenshots {shots.length ? `(${shots.length})` : ""}
                      </div>
                      <ScreenshotCarousel shots={shots} />
                    </div>
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
