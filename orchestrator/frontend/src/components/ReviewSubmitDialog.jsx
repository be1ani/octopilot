import { useEffect, useMemo, useState } from "react";
import { FiX } from "react-icons/fi";
import { ScreenshotCarousel } from "./ScreenshotCarousel.jsx";
import { formatRelativeTime, urlHostname } from "../format.js";
import "./ReviewSubmitDialog.css";

const STATUSES = ["In progress", "Finished", "Submitted", "Not found", "Failed"];

export function ReviewSubmitDialog({ record, onClose, onSubmitted, onRemoveMachine }) {
  const [status, setStatus] = useState(record?.status || "Finished");
  const [applicationUrl, setApplicationUrl] = useState(record?.application_url || "");
  const [jobTitle, setJobTitle] = useState(record?.job_title || "");
  const [jobCompany, setJobCompany] = useState(record?.job_company || "");
  const [jobCity, setJobCity] = useState(record?.job_city || "");
  const [description, setDescription] = useState(record?.description || "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    setStatus(record?.status || "Finished");
    setApplicationUrl(record?.application_url || "");
    setJobTitle(record?.job_title || "");
    setJobCompany(record?.job_company || "");
    setJobCity(record?.job_city || "");
    setDescription(record?.description || "");
  }, [record?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose?.();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const fields = useMemo(
    () => (record?.fields && typeof record.fields === "object" ? record.fields : {}),
    [record]
  );
  const fieldKeys = Object.keys(fields);

  const submit = async () => {
    if (!record?.id) return;
    setBusy(true);
    setErr(null);
    try {
      const res = await fetch(`/api/applications/${record.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          status: status || "Finished",
          application_url: applicationUrl,
          job_title: jobTitle,
          job_company: jobCompany,
          job_city: jobCity,
          description,
          reviewed: true,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || res.statusText || "Failed to submit");
      onSubmitted?.(data);
      onClose?.();
    } catch (e) {
      setErr(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!record) return null;

  const shots = Array.isArray(record.screenshots) ? record.screenshots : [];

  return (
    <div
      className="review-backdrop"
      role="presentation"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose?.();
      }}
    >
      <div
        className="review-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="review-title"
      >
        <header className="review-head">
          <div>
            <h2 id="review-title" className="review-title">
              Review &amp; submit application
            </h2>
            <div className="review-sub muted">
              Confirm the final state and description recorded by the agent.
            </div>
          </div>
          <button
            type="button"
            className="review-close"
            onClick={onClose}
            aria-label="Close"
          >
            <FiX />
          </button>
        </header>

        <section className="review-meta">
          {record.job_title || record.job_company ? (
            <div className="review-job">
              <div className="review-job-title">
                {record.job_title || record.job_company}
              </div>
              <div className="review-job-sub muted">
                {record.job_title && record.job_company ? record.job_company : ""}
                {record.job_title && record.job_company && record.job_city ? " · " : ""}
                {record.job_city || ""}
                {(record.job_title || record.job_company) && urlHostname(record.application_url)
                  ? " · "
                  : ""}
                {urlHostname(record.application_url) ? (
                  record.application_url ? (
                    <a
                      className="review-host"
                      href={record.application_url}
                      target="_blank"
                      rel="noreferrer"
                      title={record.application_url}
                    >
                      {urlHostname(record.application_url)}
                    </a>
                  ) : (
                    <span>{urlHostname(record.application_url)}</span>
                  )
                ) : null}
              </div>
            </div>
          ) : record.application_url ? (
            <a
              className="review-url mono"
              href={record.application_url}
              target="_blank"
              rel="noreferrer"
            >
              {record.application_url}
            </a>
          ) : null}
          <div className="review-meta-row mono muted">
            <span title={record.created_at_iso || ""}>
              {record.created_at_iso
                ? formatRelativeTime(record.created_at_iso)
                : ""}
            </span>
            {record.duration_label ? <span>· {record.duration_label}</span> : null}
            {record.llm_cost_usd != null ? (
              <span>· ${Number(record.llm_cost_usd).toFixed(4)}</span>
            ) : null}
            {record.llm_tokens != null ? (
              <span>· {Number(record.llm_tokens).toLocaleString()} tok</span>
            ) : null}
            {record.llm_model ? <span>· {record.llm_model}</span> : null}
          </div>
        </section>

        <section className="review-body">
          <label className="review-field review-field--full">
            <span>Application URL</span>
            <input
              type="url"
              value={applicationUrl}
              onChange={(e) => setApplicationUrl(e.target.value)}
              placeholder="https://…"
            />
          </label>

          <div className="review-row-3">
            <label className="review-field">
              <span>Job title</span>
              <input value={jobTitle} onChange={(e) => setJobTitle(e.target.value)} placeholder="(optional)" />
            </label>
            <label className="review-field">
              <span>Company</span>
              <input value={jobCompany} onChange={(e) => setJobCompany(e.target.value)} placeholder="(optional)" />
            </label>
            <label className="review-field">
              <span>City</span>
              <input value={jobCity} onChange={(e) => setJobCity(e.target.value)} placeholder="(optional)" />
            </label>
          </div>

          <label className="review-field">
            <span>Status</span>
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>

          <label className="review-field review-field--full">
            <span>Description / result</span>
            <textarea
              rows={6}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Summary of what the agent did, filed values, next steps…"
            />
          </label>

          <div className="review-field review-field--full">
            <span className="review-field-label">
              Filled fields {fieldKeys.length ? `(${fieldKeys.length})` : ""}
            </span>
            {fieldKeys.length ? (
              <div className="review-fields-table" role="table">
                {fieldKeys.map((k) => (
                  <div key={k} className="review-fields-row" role="row">
                    <div className="review-fields-key mono" role="cell">
                      {k}
                    </div>
                    <div className="review-fields-val mono" role="cell">
                      {typeof fields[k] === "string"
                        ? fields[k]
                        : JSON.stringify(fields[k])}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div className="muted review-fields-empty">
                No form fields were recorded for this run.
              </div>
            )}
          </div>

          <div className="review-field review-field--full">
            <span className="review-field-label">
              Screenshots {shots.length ? `(${shots.length})` : ""}
            </span>
            <ScreenshotCarousel shots={shots} />
          </div>
        </section>

        {err ? <div className="review-error">{err}</div> : null}

        <footer className="review-foot">
          <button
            type="button"
            className="btn ghost"
            onClick={onClose}
            disabled={busy}
          >
            Cancel
          </button>
          {onRemoveMachine ? (
            <button
              type="button"
              className="btn ghost review-remove"
              onClick={async () => {
                if (!confirm("Submit and remove this machine?")) return;
                await submit();
                onRemoveMachine?.();
              }}
              disabled={busy}
              title="Submit and remove the machine container"
            >
              Submit &amp; remove
            </button>
          ) : null}
          <button
            type="button"
            className="btn primary"
            onClick={submit}
            disabled={busy}
          >
            {busy ? "Submitting…" : "Submit"}
          </button>
        </footer>
      </div>
    </div>
  );
}
