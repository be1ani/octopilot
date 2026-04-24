import { useEffect, useMemo, useState } from "react";
import { ScreenshotCarousel } from "./ScreenshotCarousel.jsx";
import { formatRelativeTime, urlHostname } from "../format.js";
import { llmKeyHeaders } from "../llmKeys.js";
import "./ApplicationsPage.css";

function StatusPill({ status }) {
  const cls =
    status === "Finished"
      ? "ok"
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
  // Re-render once a minute so "3 min ago" stays fresh without manual refresh.
  const [tick, setTick] = useState(0);

  const refresh = async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/applications?limit=500", { headers: llmKeyHeaders() });
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

  useEffect(() => {
    refresh();
  }, []);

  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 60_000);
    return () => clearInterval(id);
  }, []);

  const rows = useMemo(() => (Array.isArray(apps) ? apps : []), [apps]);
  const now = useMemo(() => new Date(), [tick]);

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
            const id = r?.id || `${r?.machine_id || "m"}:${r?.created_at_iso || ""}:${r?.application_url || ""}`;
            const open = expandedId === id;
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
            return (
              <div key={id} className={`apps-row ${open ? "apps-row--open" : ""}`}>
                <div className="apps-when" title={createdIso || ""}>{relWhen || "—"}</div>
                <div>
                  <StatusPill status={r?.status} />
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
                  <button type="button" className="btn ghost btn--small" onClick={() => setExpandedId(open ? null : id)}>
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
