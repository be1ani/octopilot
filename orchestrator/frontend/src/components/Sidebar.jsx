import { useEffect, useState } from "react";
import octoLogo from "../octo.svg";
import { CHATGPT_MODEL_IDS, DEFAULT_CHATGPT_MODEL } from "../chatgptModels.js";
import "./Sidebar.css";

export function Sidebar({
  health,
  stats,
  loading,
  error,
  outOfFundsNeeded,
  outOfFundsText,
  onIgnoreOutOfFunds,
  onStart,
  onRefresh,
  minimized,
  onToggleMinimize,
  view,
  onChangeView,
  settings,
  onUpdateSettings,
}) {
  const [url, setUrl] = useState("");
  const [profileId, setProfileId] = useState("");
  const [profiles, setProfiles] = useState([]);
  const [profilesBusy, setProfilesBusy] = useState(false);
  const [llmModel, setLlmModel] = useState(DEFAULT_CHATGPT_MODEL);
  const [busy, setBusy] = useState(false);
  const [startModalOpen, setStartModalOpen] = useState(false);
  const [queueModelBusy, setQueueModelBusy] = useState(false);

  const loadProfiles = async () => {
    setProfilesBusy(true);
    try {
      const res = await fetch("/api/profiles");
      const data = await res.json().catch(() => ({}));
      const list = Array.isArray(data?.profiles) ? data.profiles : [];
      setProfiles(list);
      // If no selection yet, default to first profile id (if any).
      if (!profileId.trim() && list.length) {
        const first = String(list[0]?.profile_id || "").trim();
        if (first) setProfileId(first);
      }
    } catch {
      setProfiles([]);
    } finally {
      setProfilesBusy(false);
    }
  };

  const queueModel = settings?.default_llm_model || "";
  const queueModelOptions = queueModel && !CHATGPT_MODEL_IDS.includes(queueModel)
    ? [queueModel, ...CHATGPT_MODEL_IDS]
    : CHATGPT_MODEL_IDS;

  const maxParallel = Number(settings?.max_parallel_machines || 0);
  const activeCount = Number(stats?.machines_running || 0) + Number(stats?.machines_starting || 0);
  const parallelFull = maxParallel > 0 && activeCount >= maxParallel;
  const budgetExceeded = Boolean(stats?.budget_exceeded);
  const startBlocked = parallelFull || budgetExceeded || !health?.docker;
  const startBlockedTitle = !health?.docker
    ? "Docker is not available"
    : budgetExceeded
      ? "Budget exceeded — raise budget in Settings"
      : parallelFull
        ? `Max parallel machines reached (${activeCount}/${maxParallel})`
        : "Start job";

  const onChangeQueueModel = async (next) => {
    if (!onUpdateSettings || !next || next === queueModel) return;
    setQueueModelBusy(true);
    try {
      await onUpdateSettings({ default_llm_model: next });
    } catch {
      /* error surfaced via parent banner */
    } finally {
      setQueueModelBusy(false);
    }
  };

  useEffect(() => {
    if (!startModalOpen) return;
    const onKey = (e) => {
      if (e.key === "Escape") setStartModalOpen(false);
    };
    loadProfiles();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [startModalOpen]);

  const submit = async (e) => {
    e.preventDefault();
    if (!url.trim()) return;
    setBusy(true);
    try {
      await onStart({
        url: url.trim(),
        llm_model: llmModel,
        ...(profileId.trim() ? { profile_id: profileId.trim() } : {}),
      });
      setUrl("");
      setStartModalOpen(false);
    } finally {
      setBusy(false);
    }
  };

  const startModal = startModalOpen ? (
    <div
      className="modal-backdrop"
      role="presentation"
      onClick={(ev) => {
        if (ev.target === ev.currentTarget) setStartModalOpen(false);
      }}
    >
      <div
        className="modal-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="start-job-modal-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h2 id="start-job-modal-title">Start machine</h2>
          <button
            type="button"
            className="modal-close"
            onClick={() => setStartModalOpen(false)}
            aria-label="Close"
          >
            ×
          </button>
        </div>
        <form className="modal-form" onSubmit={submit}>
          <label className="modal-field">
            <span>Job application URL</span>
            <input
              type="url"
              name="url"
              placeholder="https://…"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              required
              autoFocus
            />
          </label>
          <label className="modal-field">
            <span>Profile</span>
            <select
              name="profile_id"
              value={profileId}
              onChange={(e) => setProfileId(e.target.value)}
              aria-label="Profile"
              disabled={profilesBusy}
            >
              <option value="">{profilesBusy ? "Loading…" : "Server default"}</option>
              {profiles.map((p) => (
                <option key={p.profile_id} value={p.profile_id}>
                  {p.profile_id}
                  {p.label ? ` — ${p.label}` : ""}
                </option>
              ))}
            </select>
          </label>
          <label className="modal-field">
            <span>ChatGPT model (OpenAI)</span>
            <select
              name="llm_model"
              value={llmModel}
              onChange={(e) => setLlmModel(e.target.value)}
              aria-label="OpenAI ChatGPT model"
            >
              {CHATGPT_MODEL_IDS.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </label>
          <div className="modal-actions">
            <button
              type="button"
              className="btn ghost"
              onClick={() => setStartModalOpen(false)}
            >
              Cancel
            </button>
            <button type="submit" className="btn primary" disabled={busy || !health?.docker}>
              {busy ? "Starting…" : "Start agent"}
            </button>
          </div>
        </form>
      </div>
    </div>
  ) : null;

  if (minimized) {
    return (
      <>
        <header className="topbar topbar--minimized" aria-label="Orchestrator (minimized)">
          <img className="topbar-logo-img topbar-logo-img--min" src={octoLogo} alt="OctoPilot" />
          {loading && !stats ? (
            <span className="topbar-min-mono">…</span>
          ) : stats ? (
            <div
              className="topbar-min-stats"
              title="Apps · $ total · running · starting · machines"
            >
              <span className="topbar-min-mono">{stats.applications_submitted}</span>
              <span className="topbar-min-mono">${Number(stats.total_cost_usd).toFixed(2)}</span>
              <span className="topbar-min-mono">{stats.machines_running}</span>
              <span className="topbar-min-mono">{stats.machines_starting}</span>
              <span className="topbar-min-mono">{stats.machines_total}</span>
            </div>
          ) : null}
          <nav className="topbar-nav topbar-nav--min" aria-label="Views">
            <button
              type="button"
              className={`btn nav-btn ${view === "machines" ? "btn--active" : ""}`}
              onClick={() => onChangeView?.("machines")}
              title="Machines"
            >
              Machines
            </button>
            <button
              type="button"
              className={`btn nav-btn ${view === "applications" ? "btn--active" : ""}`}
              onClick={() => onChangeView?.("applications")}
              title="Past applications"
            >
              Apps
            </button>
            <button
              type="button"
              className={`btn nav-btn ${view === "profiles" ? "btn--active" : ""}`}
              onClick={() => onChangeView?.("profiles")}
              title="Profiles"
            >
              Profiles
            </button>
            <button
              type="button"
              className={`btn nav-btn ${view === "settings" ? "btn--active" : ""}`}
              onClick={() => onChangeView?.("settings")}
              title="Settings"
            >
              Settings
            </button>
          </nav>
          <div className="topbar-min-spacer" aria-hidden />
          <div className="topbar-right topbar-right--min">
            {health?.image ? (
              <div className="topbar-image-tag mono subtle" title={health.image}>
                {health.image}
              </div>
            ) : null}
            <div className="topbar-toolbar-box">
              {settings ? (
                <label
                  className="topbar-queue-model topbar-queue-model--min"
                  title="Model used for jobs dispatched from the queue"
                >
                  <span className="topbar-queue-model-label">Queue</span>
                  <select
                    className="topbar-queue-model-select"
                    value={queueModel}
                    disabled={queueModelBusy}
                    onChange={(e) => onChangeQueueModel(e.target.value)}
                    aria-label="Default ChatGPT model for queued jobs"
                  >
                    {queueModelOptions.map((id) => (
                      <option key={id} value={id}>
                        {id}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}
              <button
                type="button"
                className="btn primary btn--toolbar"
                onClick={() => setStartModalOpen(true)}
                disabled={startBlocked}
                title={startBlockedTitle}
              >
                + Job
              </button>
              <button type="button" className="btn ghost btn--toolbar" onClick={onRefresh}>
                Refresh
              </button>
              <button
                type="button"
                className="topbar-expand"
                onClick={onToggleMinimize}
                title="Expand toolbar"
                aria-label="Expand toolbar"
              >
                ▾
              </button>
            </div>
          </div>
        </header>
        {startModal}
      </>
    );
  }

  return (
    <>
      <header className="topbar">
        <div className="topbar-main">
          <div className="topbar-brand-block">
            <div className="topbar-brand">
              <img className="topbar-logo-img" src={octoLogo} alt="OctoPilot" />
              <div className="topbar-titles">
                <span className="topbar-title">OctoPilot</span>
                <span className="topbar-tag">Orchestrator</span>
              </div>
            </div>
            {loading && !stats ? (
              <span className="muted topbar-loading">Loading…</span>
            ) : stats ? (
              <ul className="stat-strip" aria-label="Global statistics">
                <li>
                  <span className="stat-strip-label">Apps</span>
                  <span className="stat-strip-val">{stats.applications_submitted}</span>
                </li>
                <li>
                  <span className="stat-strip-label">Cost</span>
                  <span className="stat-strip-val mono">${Number(stats.total_cost_usd).toFixed(2)}</span>
                </li>
                <li>
                  <span className="stat-strip-label">Run</span>
                  <span className="stat-strip-val">{stats.machines_running}</span>
                </li>
                <li>
                  <span className="stat-strip-label">Start</span>
                  <span className="stat-strip-val">{stats.machines_starting}</span>
                </li>
                <li>
                  <span className="stat-strip-label">Total</span>
                  <span className="stat-strip-val">{stats.machines_total}</span>
                </li>
                <li>
                  <span className="stat-strip-label">$/app</span>
                  <span className="stat-strip-val mono" title="Average cost per application (total cost ÷ apps)">
                    {Number(stats.applications_submitted) > 0
                      ? `$${(Number(stats.total_cost_usd) / Number(stats.applications_submitted)).toFixed(3)}/app`
                      : "—"}
                  </span>
                </li>
              </ul>
            ) : null}
            <nav className="topbar-nav" aria-label="Views">
              <button
                type="button"
                className={`btn nav-btn ${view === "machines" ? "btn--active" : ""}`}
                onClick={() => onChangeView?.("machines")}
              >
                Machines
              </button>
              <button
                type="button"
                className={`btn nav-btn ${view === "applications" ? "btn--active" : ""}`}
                onClick={() => onChangeView?.("applications")}
              >
                Past applications
              </button>
              <button
                type="button"
                className={`btn nav-btn ${view === "profiles" ? "btn--active" : ""}`}
                onClick={() => onChangeView?.("profiles")}
              >
                Profiles
              </button>
              <button
                type="button"
                className={`btn nav-btn ${view === "settings" ? "btn--active" : ""}`}
                onClick={() => onChangeView?.("settings")}
              >
                Settings
              </button>
            </nav>
          </div>
          <div className="topbar-right">
            {health?.image ? (
              <div className="topbar-image-tag mono subtle" title={health.image}>
                {health.image}
              </div>
            ) : null}
            <div className="topbar-toolbar-box">
              {settings ? (
                <label
                  className="topbar-queue-model"
                  title="Model used for jobs dispatched from the queue"
                >
                  <span className="topbar-queue-model-label">Queue model</span>
                  <select
                    className="topbar-queue-model-select"
                    value={queueModel}
                    disabled={queueModelBusy}
                    onChange={(e) => onChangeQueueModel(e.target.value)}
                    aria-label="Default ChatGPT model for queued jobs"
                  >
                    {queueModelOptions.map((id) => (
                      <option key={id} value={id}>
                        {id}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}
              <button
                type="button"
                className="btn primary"
                onClick={() => setStartModalOpen(true)}
                disabled={startBlocked}
                title={startBlockedTitle}
              >
                Start job
              </button>
              <button type="button" className="btn ghost" onClick={onRefresh}>
                Refresh
              </button>
              <button
                type="button"
                className="topbar-collapse"
                onClick={onToggleMinimize}
                title="Minimize toolbar"
                aria-label="Minimize toolbar"
              >
                ▴
              </button>
            </div>
          </div>
        </div>

        {health && !health.docker && (
          <div className="banner warn">
            <strong>Docker check failed.</strong>{" "}
            {health.docker_detail?.error ? (
              <span className="docker-err">{health.docker_detail.error}</span>
            ) : (
              <span>Start Docker, then refresh.</span>
            )}
            {health.docker_detail?.docker_binary ? (
              <span className="docker-meta">
                {" "}
                (CLI: <code>{health.docker_detail.docker_binary}</code>)
              </span>
            ) : (
              <span className="docker-meta">
                {" "}
                Set <code>ORCH_DOCKER_BIN</code> or fix <code>PATH</code>.
              </span>
            )}
          </div>
        )}

        {outOfFundsNeeded && (
          <div className="banner danger">
            <strong>Out of LLM funds.</strong>{" "}
            <span>{outOfFundsText || "The configured LLM provider/model ran out of funds (insufficient quota)."}</span>{" "}
            <button type="button" className="btn ghost btn--small" onClick={() => onIgnoreOutOfFunds?.()}>
              Ignore
            </button>
          </div>
        )}

        {error && <div className="banner danger">{error}</div>}
      </header>
      {startModal}
    </>
  );
}
