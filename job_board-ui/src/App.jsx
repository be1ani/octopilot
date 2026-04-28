import { useEffect, useMemo, useRef, useState } from "react";
import { jobBoardApi, orchApi } from "./api.js";
import { sanitizeHtml } from "./sanitizeHtml.js";

function Button({ children, onClick, disabled, variant = "primary", title }) {
  return (
    <button
      className={`btn btn-${variant}`}
      onClick={onClick}
      disabled={disabled}
      title={title}
      type="button"
    >
      {children}
    </button>
  );
}

function Field({ label, value, onChange, placeholder }) {
  return (
    <label className="field">
      <div className="field-label">{label}</div>
      <input className="input" value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} />
    </label>
  );
}

function QueuePage({
  items,
  jobById,
  loading,
  busy,
  maxParallel,
  onRefresh,
  onStart,
  onDequeue,
  onChangePriority,
  onSetPriority,
  onSetStatus,
}) {
  const groups = useMemo(() => {
    const g = { pending: [], in_progress: [], done: [], error: [] };
    for (const it of items) {
      const s = it.status || "pending";
      if (!g[s]) g[s] = [];
      g[s].push(it);
    }
    // Sort pending by priority desc, then created_at asc
    g.pending.sort((a, b) => {
      if (b.priority !== a.priority) return b.priority - a.priority;
      return String(a.created_at).localeCompare(String(b.created_at));
    });
    // Sort others by updated_at desc
    for (const k of ["in_progress", "done", "error"]) {
      g[k].sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
    }
    return g;
  }, [items]);

  function renderRow(item, section) {
    const job = jobById.get(item.job_id);
    const title = job ? job.title : `Job ${item.job_id}`;
    const company = job ? job.company : "Unknown company";
    const city = job?.city;
    const applyHref = job ? (job.apply_url && String(job.apply_url).trim()) || job.url : "";
    return (
      <div key={item.id} className="card queue-card">
        <div className="card-top">
          <div>
            <div className="card-title">
              {applyHref ? (
                <a
                  className="card-title-link"
                  href={applyHref}
                  target="_blank"
                  rel="noreferrer"
                  title="Open job posting in a new tab"
                >
                  {title}
                </a>
              ) : (
                title
              )}
            </div>
            <div className="card-sub">
              {company}
              {city ? (
                <>
                  {" · "}
                  <span className="job-city">{city}</span>
                </>
              ) : null}
              {item.profile_id ? (
                <>
                  {" · profile: "}
                  <span className="queue-profile">{item.profile_id}</span>
                </>
              ) : null}
            </div>
          </div>
          <div className="card-actions queue-actions">
            {section === "pending" ? (
              <>
                <div className="queue-priority" title="Priority (higher runs first)">
                  <button
                    type="button"
                    className="btn btn-secondary queue-pri-btn"
                    disabled={busy}
                    aria-label="Increase priority"
                    onClick={() => onChangePriority(item, 1)}
                  >
                    ▲
                  </button>
                  <input
                    type="number"
                    className="input queue-pri-input"
                    value={item.priority}
                    onChange={(e) => onSetPriority(item, e.target.value)}
                    disabled={busy}
                    aria-label="Priority"
                  />
                  <button
                    type="button"
                    className="btn btn-secondary queue-pri-btn"
                    disabled={busy}
                    aria-label="Decrease priority"
                    onClick={() => onChangePriority(item, -1)}
                  >
                    ▼
                  </button>
                </div>
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busy}
                  onClick={() => onStart(item)}
                  title="Start this item now, bypassing the parallel limit"
                >
                  Start now
                </button>
                <button
                  type="button"
                  className="btn btn-danger"
                  disabled={busy}
                  onClick={() => onDequeue(item)}
                >
                  Dequeue
                </button>
              </>
            ) : null}
            {section === "in_progress" ? (
              <>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={busy}
                  onClick={() => onSetStatus(item, "done")}
                  title="Mark this item as done"
                >
                  Mark done
                </button>
                <button
                  type="button"
                  className="btn btn-danger"
                  disabled={busy}
                  onClick={() => onDequeue(item)}
                >
                  Dequeue
                </button>
              </>
            ) : null}
            {section === "done" || section === "error" ? (
              <button
                type="button"
                className="btn btn-danger"
                disabled={busy}
                onClick={() => onDequeue(item)}
              >
                Dequeue
              </button>
            ) : null}
          </div>
        </div>
        <div className="queue-meta">
          {section === "pending" ? (
            <span className="queue-meta-item">
              priority: <strong>{item.priority}</strong>
            </span>
          ) : null}
          {item.machine_id ? (
            <span className="queue-meta-item">machine: {item.machine_id}</span>
          ) : null}
          {item.started_at ? (
            <span className="queue-meta-item">started: {item.started_at}</span>
          ) : null}
          {item.finished_at ? (
            <span className="queue-meta-item">finished: {item.finished_at}</span>
          ) : null}
          {item.error ? (
            <span className="queue-meta-item queue-meta-error">error: {item.error}</span>
          ) : null}
        </div>
      </div>
    );
  }

  const sections = [
    { key: "pending", label: "Pending", list: groups.pending },
    { key: "in_progress", label: "In progress", list: groups.in_progress },
    { key: "done", label: "Done", list: groups.done },
  ];
  if (groups.error && groups.error.length > 0) {
    sections.push({ key: "error", label: "Error", list: groups.error });
  }

  const inProgressCount = groups.in_progress.length;

  return (
    <section className="panel">
      <div className="panel-head">
        <div className="panel-title">Queue</div>
        <div className="row" style={{ marginTop: 0 }}>
          <button type="button" className="btn btn-secondary" onClick={onRefresh} disabled={busy}>
            Refresh
          </button>
        </div>
      </div>
      <div className="queue-hint muted">
        The orchestrator automatically picks up pending items (highest priority first) whenever a
        machine slot frees up.{" "}
        <strong>
          {inProgressCount}/{maxParallel}
        </strong>{" "}
        slots in use. Change the limit from the top bar.
      </div>
      {loading && items.length === 0 ? <div className="muted">Loading…</div> : null}
      {items.length === 0 && !loading ? (
        <div className="muted">Queue is empty. Add jobs from the Jobs tab.</div>
      ) : null}
      {sections.map((s) => (
        <div className="queue-section" key={s.key}>
          <div className={`queue-section-head queue-section-${s.key}`}>
            <span className="queue-section-label">{s.label}</span>
            <span className="queue-section-count">{s.list.length}</span>
          </div>
          {s.list.length === 0 ? (
            <div className="muted queue-empty">No items.</div>
          ) : (
            <div className="cards">{s.list.map((it) => renderRow(it, s.key))}</div>
          )}
        </div>
      ))}
    </section>
  );
}

function EnqueueButton({ job, profiles, defaultProfileId, onEnqueue, busy }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e) {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false);
    }
    function onEsc(e) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onEsc);
    };
  }, [open]);

  const list =
    profiles.length > 0
      ? profiles
      : defaultProfileId
      ? [{ profile_id: defaultProfileId }]
      : [];

  const defaultLabel = defaultProfileId || "default";

  return (
    <div className="enq-split" ref={wrapRef}>
      <button
        type="button"
        className="btn btn-primary enq-main"
        disabled={busy}
        title={`Queue with profile: ${defaultLabel}`}
        onClick={() => onEnqueue(job, defaultProfileId)}
      >
        Add to queue
      </button>
      <button
        type="button"
        className="btn btn-primary enq-caret"
        disabled={busy || list.length === 0}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Choose profile"
        onClick={() => setOpen((v) => !v)}
      >
        ▾
      </button>
      {open ? (
        <div className="enq-menu" role="menu">
          {list.map((p) => (
            <button
              key={p.profile_id}
              type="button"
              role="menuitem"
              className="enq-menu-item"
              onClick={() => {
                setOpen(false);
                onEnqueue(job, p.profile_id);
              }}
            >
              <span className="enq-menu-id">{p.profile_id}</span>
              {p.label ? <span className="enq-menu-label"> — {p.label}</span> : null}
              {p.profile_id === defaultProfileId ? (
                <span className="enq-menu-default">default</span>
              ) : null}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState("jobs"); // jobs | queue | applications
  const [jobs, setJobs] = useState([]);
  const [apps, setApps] = useState([]);
  const [orchApps, setOrchApps] = useState([]);
  const [queueItems, setQueueItems] = useState([]);
  const [profiles, setProfiles] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const [selectedProfile, setSelectedProfile] = useState("main");
  const [maxParallel, setMaxParallel] = useState(4);
  const [maxParallelDraft, setMaxParallelDraft] = useState("4");

  const [addJobOpen, setAddJobOpen] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const [newCompany, setNewCompany] = useState("");
  const [newUrl, setNewUrl] = useState("");
  /** job id → whether description is expanded */
  const [jobDescOpen, setJobDescOpen] = useState({});

  const jobById = useMemo(() => {
    const m = new Map();
    for (const j of jobs) m.set(j.id, j);
    return m;
  }, [jobs]);

  // Map each job id to its most "advanced" current queue status so we can
  // decorate job cards with a colored left border.
  const queueStatusByJob = useMemo(() => {
    // Rank is used to pick the best (most advanced) status if a job has multiple
    // queue items over its lifetime. Order: in_progress > pending > done > error.
    const rank = { error: 0, done: 1, pending: 2, in_progress: 3 };
    const best = new Map();
    for (const it of queueItems) {
      const jid = it.job_id;
      const st = it.status || "pending";
      const prev = best.get(jid);
      if (!prev || (rank[st] ?? -1) > (rank[prev] ?? -1)) best.set(jid, st);
    }
    return best;
  }, [queueItems]);

  // Group finished orchestrator applications by normalized company name so we
  // can show "other applications at {company}" under each job card.
  const orchAppsByCompany = useMemo(() => {
    const key = (s) => String(s || "").trim().toLowerCase();
    const m = new Map();
    for (const a of orchApps) {
      const c = key(a?.job_company);
      if (!c) continue;
      if (!m.has(c)) m.set(c, []);
      m.get(c).push(a);
    }
    for (const arr of m.values()) {
      arr.sort((a, b) =>
        String(b?.created_at_iso || "").localeCompare(String(a?.created_at_iso || ""))
      );
    }
    return m;
  }, [orchApps]);

  async function refreshAll() {
    setLoading(true);
    try {
      const [j, a, q, oa] = await Promise.all([
        jobBoardApi.listJobs(),
        jobBoardApi.listApplications(),
        jobBoardApi.listQueue().catch(() => []),
        orchApi.listApplications().catch(() => [])
      ]);
      setJobs(j);
      setApps(a);
      setQueueItems(Array.isArray(q) ? q : []);
      setOrchApps(Array.isArray(oa) ? oa : []);
      setError(null);
    } catch (e) {
      setError(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }

  async function refreshQueue() {
    try {
      const q = await jobBoardApi.listQueue();
      setQueueItems(Array.isArray(q) ? q : []);
    } catch (e) {
      setError(e?.message || String(e));
    }
  }

  async function refreshProfiles() {
    try {
      const res = await orchApi.listProfiles();
      const list = Array.isArray(res?.profiles) ? res.profiles : [];
      setProfiles(list);
      if (list.length > 0) {
        const exists = list.some((p) => p.profile_id === selectedProfile);
        if (!exists) setSelectedProfile(list[0].profile_id);
      }
    } catch (e) {
      setProfiles([]);
    }
  }

  useEffect(() => {
    refreshAll();
    refreshSettings();
  }, []);

  useEffect(() => {
    refreshProfiles();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Auto-refresh the queue every 4s while the Queue tab is visible, so
  // pending → in_progress → done transitions (driven by the orchestrator
  // dispatcher) show up without the user having to click Refresh.
  useEffect(() => {
    if (tab !== "queue") return;
    const id = setInterval(() => {
      refreshQueue();
    }, 4000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  async function refreshSettings() {
    try {
      const res = await orchApi.getSettings();
      const mp = Number(res?.settings?.max_parallel_machines);
      if (Number.isFinite(mp)) {
        setMaxParallel(mp);
        setMaxParallelDraft(String(mp));
      }
    } catch (_) {
      // Orchestrator may not be reachable; keep current default.
    }
  }

  async function applyMaxParallel(value) {
    const next = parseInt(value, 10);
    if (!Number.isFinite(next) || next < 0 || next > 64) {
      setMaxParallelDraft(String(maxParallel));
      return;
    }
    if (next === maxParallel) return;
    try {
      const res = await orchApi.updateSettings({ max_parallel_machines: next });
      const mp = Number(res?.settings?.max_parallel_machines);
      if (Number.isFinite(mp)) {
        setMaxParallel(mp);
        setMaxParallelDraft(String(mp));
      }
      setError(null);
    } catch (e) {
      setError(e?.message || String(e));
      setMaxParallelDraft(String(maxParallel));
    }
  }

  async function onCreateJob() {
    setBusy(true);
    try {
      await jobBoardApi.createJob({
        title: newTitle,
        company: newCompany,
        url: newUrl,
        description: ""
      });
      setNewTitle("");
      setNewCompany("");
      setNewUrl("");
      setAddJobOpen(false);
      await refreshAll();
      setTab("jobs");
    } catch (e) {
      setError(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  function closeAddJobModal() {
    setAddJobOpen(false);
  }

  function toggleJobDescription(jobId) {
    setJobDescOpen((prev) => ({ ...prev, [jobId]: !prev[jobId] }));
  }

  async function onEnqueue(job, profileIdOverride) {
    const profile_id = profileIdOverride || selectedProfile;
    setBusy(true);
    try {
      await jobBoardApi.createQueueItem({
        job_id: job.id,
        profile_id,
        priority: 0
      });
      setError(null);
      await refreshQueue();
    } catch (e) {
      setError(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onStartQueueItem(item) {
    const job = jobById.get(item.job_id);
    if (!job) {
      setError(`Cannot start: job ${item.job_id} not found.`);
      return;
    }
    const applyUrl = (job.apply_url && String(job.apply_url).trim()) || job.url;
    setBusy(true);
    try {
      const machine = await orchApi.enqueue({
        url: applyUrl,
        profile_id: item.profile_id || selectedProfile
      });
      await jobBoardApi.updateQueueItem(item.id, {
        status: "in_progress",
        machine_id: machine?.id || ""
      });
      setError(null);
      await refreshQueue();
    } catch (e) {
      setError(e?.message || String(e));
      try {
        await jobBoardApi.updateQueueItem(item.id, {
          status: "error",
          error: e?.message || String(e)
        });
        await refreshQueue();
      } catch (_) {}
    } finally {
      setBusy(false);
    }
  }

  async function onChangePriority(item, delta) {
    const next = (Number.isFinite(item.priority) ? item.priority : 0) + delta;
    setBusy(true);
    try {
      await jobBoardApi.updateQueueItem(item.id, { priority: next });
      await refreshQueue();
    } catch (e) {
      setError(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onSetPriority(item, value) {
    const next = parseInt(value, 10);
    if (!Number.isFinite(next)) return;
    setBusy(true);
    try {
      await jobBoardApi.updateQueueItem(item.id, { priority: next });
      await refreshQueue();
    } catch (e) {
      setError(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onSetQueueStatus(item, status) {
    setBusy(true);
    try {
      await jobBoardApi.updateQueueItem(item.id, { status });
      await refreshQueue();
    } catch (e) {
      setError(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onDequeue(item) {
    if (!confirm("Remove this item from the queue?")) return;
    setBusy(true);
    try {
      await jobBoardApi.deleteQueueItem(item.id);
      await refreshQueue();
    } catch (e) {
      setError(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="page">
      <header className="topbar">
        <div className="brand">
          <div className="brand-title">Johire Job Board</div>
          <div className="brand-sub">Jobs → Queue → Applications</div>
        </div>
        <div className="topbar-right">
          <label
            className="topbar-profile"
            title="Maximum number of agent machines the orchestrator will run in parallel. Pending queue items are auto-dispatched when a slot frees up."
          >
            <span className="topbar-profile-label">Max parallel</span>
            <input
              type="number"
              className="input topbar-maxparallel"
              min={0}
              max={64}
              value={maxParallelDraft}
              onChange={(e) => setMaxParallelDraft(e.target.value)}
              onBlur={(e) => applyMaxParallel(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") e.currentTarget.blur();
              }}
            />
          </label>
          <label className="topbar-profile" title="Default profile used when queuing jobs">
            <span className="topbar-profile-label">Default profile</span>
            <select
              className="input topbar-profile-select"
              value={selectedProfile}
              onChange={(e) => setSelectedProfile(e.target.value)}
            >
              {profiles.length === 0 ? (
                <option value={selectedProfile}>{selectedProfile || "main"}</option>
              ) : (
                profiles.map((p) => (
                  <option key={p.profile_id} value={p.profile_id}>
                    {p.profile_id}
                    {p.label ? ` — ${p.label}` : ""}
                  </option>
                ))
              )}
            </select>
          </label>
          <div className="tabs">
            <button className={`tab ${tab === "jobs" ? "tab-active" : ""}`} onClick={() => setTab("jobs")} type="button">
              Jobs
            </button>
            <button
              className={`tab ${tab === "queue" ? "tab-active" : ""}`}
              onClick={() => setTab("queue")}
              type="button"
            >
              Queue
              {queueItems.length > 0 ? (
                <span className="tab-badge">{queueItems.length}</span>
              ) : null}
            </button>
            <button
              className={`tab ${tab === "applications" ? "tab-active" : ""}`}
              onClick={() => setTab("applications")}
              type="button"
            >
              Applications
            </button>
          </div>
        </div>
      </header>

      <main className="content">
        {error ? <div className="error">{error}</div> : null}

        {tab === "jobs" ? (
          <>
            <section className="panel">
              <div className="panel-head">
                <div className="panel-title">Jobs</div>
                <div className="row" style={{ marginTop: 0 }}>
                  <Button onClick={() => setAddJobOpen(true)} disabled={busy} variant="secondary">
                    Add job
                  </Button>
                  <Button onClick={refreshAll} disabled={busy} variant="secondary">
                    Refresh
                  </Button>
                </div>
              </div>
              {loading ? <div className="muted">Loading…</div> : null}
              {jobs.length === 0 && !loading ? <div className="muted">No jobs yet.</div> : null}
              <div className="cards">
                {jobs.map((j) => {
                  const applyHref = (j.apply_url && String(j.apply_url).trim()) || j.url;
                  const descSource =
                    (j.description_html && String(j.description_html).trim()) ||
                    (j.description && String(j.description).trim()) ||
                    "";
                  const hasDesc = !!descSource;
                  const isOpen = !!jobDescOpen[j.id];
                  const qStatus = queueStatusByJob.get(j.id);
                  const borderCls = qStatus ? `card-qstate card-qstate--${qStatus}` : "";
                  const companyKey = String(j.company || "").trim().toLowerCase();
                  const similar = companyKey ? orchAppsByCompany.get(companyKey) || [] : [];
                  return (
                  <div key={j.id} className={`card ${borderCls}`.trim()}>
                    <div className="card-top">
                      <div>
                        <div className="card-title">
                          <a
                            className="card-title-link"
                            href={applyHref}
                            target="_blank"
                            rel="noreferrer"
                            title="Open job posting in a new tab"
                          >
                            {j.title}
                          </a>
                        </div>
                        <div className="card-sub">
                          {j.company}
                          {j.city ? (
                            <>
                              {" · "}
                              <span className="job-city">{j.city}</span>
                            </>
                          ) : null}
                          {qStatus ? (
                            <>
                              {" · "}
                              <span className={`queue-chip queue-chip--${qStatus}`}>
                                {qStatus === "in_progress" ? "in progress" : qStatus}
                              </span>
                            </>
                          ) : null}
                        </div>
                      </div>
                      <div className="card-actions">
                        <EnqueueButton
                          job={j}
                          profiles={profiles}
                          defaultProfileId={selectedProfile}
                          onEnqueue={onEnqueue}
                          busy={busy}
                        />
                      </div>
                    </div>
                    {similar.length > 0 ? (
                      <div className="similar-apps">
                        <div className="similar-apps-head muted">
                          Other applications at {j.company}
                          <span className="similar-apps-count">{similar.length}</span>
                        </div>
                        <ul className="similar-apps-list">
                          {similar.slice(0, 5).map((a) => {
                            const title = a.job_title || (a.application_url || "").trim() || "(untitled)";
                            const city = a.job_city || "";
                            const when = a.created_at_iso
                              ? new Date(a.created_at_iso).toLocaleDateString(undefined, {
                                  year: "numeric",
                                  month: "short",
                                  day: "numeric",
                                })
                              : "";
                            return (
                              <li key={a.id} className="similar-apps-item">
                                <span className={`similar-status similar-status--${String(a.status || "").toLowerCase().replace(/\s+/g, "-")}`}>
                                  {a.status || "—"}
                                </span>
                                <span className="similar-title" title={a.application_url || ""}>
                                  {title}
                                </span>
                                {city ? <span className="similar-city">· {city}</span> : null}
                                {when ? <span className="similar-when muted">· {when}</span> : null}
                                {a.profile_id ? (
                                  <span className="similar-profile muted">· profile: {a.profile_id}</span>
                                ) : null}
                              </li>
                            );
                          })}
                        </ul>
                        {similar.length > 5 ? (
                          <div className="muted similar-apps-more">… {similar.length - 5} more</div>
                        ) : null}
                      </div>
                    ) : null}
                    {hasDesc ? (
                      <div className="job-desc-block">
                        <button
                          className="btn btn-secondary job-desc-toggle"
                          type="button"
                          onClick={() => toggleJobDescription(j.id)}
                          aria-expanded={isOpen}
                        >
                          {isOpen ? "Hide description" : "Show description"}
                        </button>
                        {isOpen ? (
                          <div
                            className="job-desc"
                            dangerouslySetInnerHTML={{ __html: sanitizeHtml(descSource) }}
                          />
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                  );
                })}
              </div>
            </section>

            {addJobOpen ? (
              <div className="modal-root" role="dialog" aria-modal="true" aria-labelledby="add-job-title">
                <div className="modal-backdrop" onClick={closeAddJobModal} />
                <div className="modal">
                  <h2 className="modal-title" id="add-job-title">
                    Add job
                  </h2>
                  <div className="modal-fields">
                    <Field label="Title" value={newTitle} onChange={setNewTitle} placeholder="Backend Engineer" />
                    <Field label="Company" value={newCompany} onChange={setNewCompany} placeholder="ACME GmbH" />
                    <Field label="URL" value={newUrl} onChange={setNewUrl} placeholder="https://company.com/jobs/123" />
                  </div>
                  <div className="modal-actions">
                    <Button
                      onClick={onCreateJob}
                      disabled={busy || !newTitle.trim() || !newCompany.trim() || !newUrl.trim()}
                      title="Creates a job in job board DB"
                    >
                      Create
                    </Button>
                    <Button onClick={closeAddJobModal} disabled={busy} variant="secondary">
                      Cancel
                    </Button>
                  </div>
                </div>
              </div>
            ) : null}
          </>
        ) : tab === "queue" ? (
          <QueuePage
            items={queueItems}
            jobById={jobById}
            loading={loading}
            busy={busy}
            maxParallel={maxParallel}
            onRefresh={refreshQueue}
            onStart={onStartQueueItem}
            onDequeue={onDequeue}
            onChangePriority={onChangePriority}
            onSetPriority={onSetPriority}
            onSetStatus={onSetQueueStatus}
          />
        ) : (
          <section className="panel">
            <div className="panel-title panel-title-only">Applications</div>
            <div className="row">
              <Button onClick={refreshAll} disabled={busy} variant="secondary">
                Refresh
              </Button>
            </div>
            {loading ? <div className="muted">Loading…</div> : null}
            {apps.length === 0 && !loading ? <div className="muted">No applications yet.</div> : null}
            <div className="cards">
              {apps.map((a) => {
                const job = jobById.get(a.job_id);
                return (
                  <div key={a.id} className="card">
                    <div className="card-top">
                      <div>
                        <div className="card-title">{job ? job.title : `Job ${a.job_id}`}</div>
                        <div className="card-sub">
                          {job ? job.company : "Unknown company"} · {a.appli_time || "no time"}
                        </div>
                      </div>
                      <div className="pill-row">
                        {a.cost != null ? <span className="pill">cost: {a.cost}</span> : null}
                        {a.duration != null ? <span className="pill">duration: {a.duration}</span> : null}
                      </div>
                    </div>
                    {Array.isArray(a.fields) && a.fields.length > 0 ? (
                      <div className="kv">
                        {a.fields.slice(0, 12).map((f, idx) => (
                          <div className="kv-row" key={`${a.id}-${idx}`}>
                            <div className="kv-k">{String(f.key)}</div>
                            <div className="kv-v">{String(f.value)}</div>
                          </div>
                        ))}
                        {a.fields.length > 12 ? <div className="muted">… {a.fields.length - 12} more</div> : null}
                      </div>
                    ) : (
                      <div className="muted">No fields captured.</div>
                    )}
                  </div>
                );
              })}
            </div>
          </section>
        )}
      </main>

      <footer className="footer">
        <span className="muted">UI calls</span>
        <code className="code">/job-board-api</code>
        <span className="muted">and</span>
        <code className="code">/orch-api</code>
      </footer>
    </div>
  );
}

