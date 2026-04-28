import { useEffect, useMemo, useState } from "react";
import "./ProfilesPage.css";

async function fetchJson(path, options) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data?.error || res.statusText || "Request failed";
    const err = new Error(msg);
    err.status = res.status;
    err.path = path;
    throw err;
  }
  return data;
}

function safeObj(v) {
  return v && typeof v === "object" && !Array.isArray(v) ? v : {};
}

function getCustomMaps(profile) {
  const other = safeObj(profile?.other);
  const custom = safeObj(other?.custom);
  const rel = safeObj(custom?.relative_fields);
  const absf = safeObj(custom?.absolute_fields);
  return { rel, absf };
}

function readFileAsText(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result || ""));
    r.onerror = () => reject(new Error("Failed to read file"));
    r.readAsText(file);
  });
}

function formatBytes(n) {
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function formatTimestamp(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleString();
  } catch {
    return "—";
  }
}

function AttachmentsCard({ profileId, busy, onError, onProfileChanged, refreshKey }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadFile, setUploadFile] = useState(null);
  const [uploadName, setUploadName] = useState("");
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadErr, setUploadErr] = useState("");

  const refresh = async () => {
    if (!profileId) {
      setRows([]);
      return;
    }
    setLoading(true);
    try {
      const data = await fetchJson(`/api/profiles/${encodeURIComponent(profileId)}/attachments`);
      setRows(Array.isArray(data?.attachments) ? data.attachments : []);
    } catch (e) {
      onError?.(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profileId, refreshKey]);

  const openUpload = () => {
    setUploadFile(null);
    setUploadName("");
    setUploadErr("");
    setUploadOpen(true);
  };

  const submitUpload = async () => {
    setUploadErr("");
    if (!uploadFile) {
      setUploadErr("Choose a file to upload.");
      return;
    }
    setUploadBusy(true);
    try {
      const fd = new FormData();
      fd.append("file", uploadFile);
      const trimmed = (uploadName || "").trim();
      if (trimmed) fd.append("name", trimmed);
      const res = await fetch(`/api/profiles/${encodeURIComponent(profileId)}/attachments`, {
        method: "POST",
        body: fd,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setUploadErr(data?.error || res.statusText || "Upload failed");
        return;
      }
      setUploadOpen(false);
      await refresh();
      onProfileChanged?.();
    } catch (e) {
      setUploadErr(e?.message || String(e));
    } finally {
      setUploadBusy(false);
    }
  };

  const removeAttachment = async (name) => {
    if (!confirm(`Delete attachment "${name}"? The file will be removed from disk.`)) return;
    try {
      await fetchJson(`/api/profiles/${encodeURIComponent(profileId)}/attachments/${encodeURIComponent(name)}`, {
        method: "DELETE",
      });
      await refresh();
      onProfileChanged?.();
    } catch (e) {
      onError?.(e?.message || String(e));
    }
  };

  const downloadHref = (name) =>
    `/api/profiles/${encodeURIComponent(profileId)}/attachments/${encodeURIComponent(name)}/download`;

  const filenameInputId = "attachments-upload-name";
  const uploadModal = uploadOpen ? (
    <div
      className="modal-backdrop"
      role="presentation"
      onClick={(ev) => {
        if (ev.target === ev.currentTarget && !uploadBusy) setUploadOpen(false);
      }}
    >
      <div className="modal-dialog profiles-modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>Upload attachment</h2>
          <button type="button" className="modal-close" onClick={() => !uploadBusy && setUploadOpen(false)} aria-label="Close">
            ×
          </button>
        </div>
        <div className="profiles-modal-body">
          {uploadErr ? <div className="banner danger">{uploadErr}</div> : null}
          <label className="profiles-field">
            <span>File</span>
            <input
              className="profiles-file"
              type="file"
              onChange={(e) => {
                const f = e.target.files?.[0] || null;
                setUploadFile(f);
                if (f && !uploadName.trim()) {
                  const stem = f.name.replace(/\.[^.]+$/, "");
                  setUploadName(stem);
                }
              }}
              disabled={uploadBusy}
            />
          </label>
          <label className="profiles-field">
            <span htmlFor={filenameInputId}>Name (shown in the table; defaults to the file name)</span>
            <input
              id={filenameInputId}
              className="profiles-input"
              type="text"
              value={uploadName}
              onChange={(e) => setUploadName(e.target.value)}
              placeholder="e.g. Resume EN"
              disabled={uploadBusy}
            />
          </label>
          <div className="modal-actions">
            <button type="button" className="btn ghost" onClick={() => setUploadOpen(false)} disabled={uploadBusy}>
              Cancel
            </button>
            <button type="button" className="btn primary" onClick={submitUpload} disabled={uploadBusy || !uploadFile}>
              {uploadBusy ? "Uploading…" : "Upload"}
            </button>
          </div>
        </div>
      </div>
    </div>
  ) : null;

  return (
    <section className="profiles-card profiles-card--wide">
      {uploadModal}
      <div className="profiles-card-head">
        <div>
          <div className="profiles-card-title">Attachments</div>
          <div className="profiles-card-sub muted">
            Documents stored under <span className="mono">attachments/{profileId}/</span> and made available to the agent at runtime.
          </div>
        </div>
        <div className="profiles-inline-actions">
          <button type="button" className="btn ghost btn--small" onClick={refresh} disabled={loading || busy}>
            {loading ? "Refreshing…" : "Refresh"}
          </button>
          <button type="button" className="btn primary btn--small" onClick={openUpload} disabled={busy}>
            + Upload
          </button>
        </div>
      </div>

      {!rows.length ? (
        <div className="profiles-empty muted">{loading ? "Loading attachments…" : "No attachments yet."}</div>
      ) : (
        <div className="profiles-table profiles-attachments">
          <div className="profiles-row profiles-row--head profiles-attachments-row">
            <div>Name</div>
            <div>Filename</div>
            <div>Size</div>
            <div>Uploaded</div>
            <div />
          </div>
          {rows.map((row) => (
            <div key={row.name} className="profiles-row profiles-attachments-row">
              <div className="profiles-k" title={row.name}>
                {row.name}
              </div>
              <div className="profiles-att-filename mono" title={row.path}>
                {row.filename}
                {row.exists === false ? <span className="profiles-att-warn"> (missing)</span> : null}
              </div>
              <div className="profiles-att-size mono">{formatBytes(row.size)}</div>
              <div className="profiles-att-time muted">{formatTimestamp(row.uploaded_at)}</div>
              <div className="profiles-actions">
                <a
                  href={downloadHref(row.name)}
                  className="btn ghost btn--small"
                  download={row.filename}
                  rel="noopener"
                >
                  Download
                </a>
                <button
                  type="button"
                  className="btn ghost btn--small"
                  onClick={() => removeAttachment(row.name)}
                  title="Delete attachment"
                >
                  Remove
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function JsonEditorCard({ profile, profileId, busy, refreshing, onRefresh, onProfileSaved }) {
  const canonical = useMemo(() => (profile ? JSON.stringify(profile, null, 2) : ""), [profile]);
  const [draft, setDraft] = useState(canonical);
  const [parseErr, setParseErr] = useState("");
  const [saveErr, setSaveErr] = useState("");
  const [saving, setSaving] = useState(false);

  // Re-sync the textarea whenever the loaded profile changes (selection, refresh,
  // post-save, or after an attachment upload). User-typed edits are intentionally
  // overwritten on external refresh; the Reset button below also restores from
  // canonical.
  useEffect(() => {
    setDraft(canonical);
    setParseErr("");
    setSaveErr("");
  }, [canonical]);

  const dirty = draft !== canonical;

  const onChange = (e) => {
    const v = e.target.value;
    setDraft(v);
    setSaveErr("");
    if (!v.trim()) {
      setParseErr("");
      return;
    }
    try {
      JSON.parse(v);
      setParseErr("");
    } catch (err) {
      setParseErr(err?.message || "Invalid JSON");
    }
  };

  const reset = () => {
    setDraft(canonical);
    setParseErr("");
    setSaveErr("");
  };

  const save = async () => {
    setSaveErr("");
    let parsed;
    try {
      parsed = JSON.parse(draft);
    } catch (err) {
      setParseErr(err?.message || "Invalid JSON");
      return;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      setSaveErr("JSON must be an object.");
      return;
    }
    setSaving(true);
    try {
      const data = await fetchJson(`/api/profiles/${encodeURIComponent(profileId)}`, {
        method: "PUT",
        body: JSON.stringify({ profile: parsed }),
      });
      onProfileSaved?.(data?.profile || parsed);
    } catch (err) {
      setSaveErr(err?.message || String(err));
    } finally {
      setSaving(false);
    }
  };

  const disabled = !profile || saving;

  return (
    <section className="profiles-card profiles-card--wide">
      <div className="profiles-card-head">
        <div>
          <div className="profiles-card-title">Profile JSON</div>
          <div className="profiles-card-sub muted">
            Edit the full profile object. Click <strong>Save</strong> to persist changes; <strong>Reset</strong> reverts the textarea to the loaded profile.
          </div>
        </div>
        <div className="profiles-inline-actions">
          <button type="button" className="btn ghost btn--small" onClick={onRefresh} disabled={busy || refreshing || saving}>
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
          <button type="button" className="btn ghost btn--small" onClick={reset} disabled={!dirty || saving}>
            Reset
          </button>
          <button
            type="button"
            className="btn primary btn--small"
            onClick={save}
            disabled={!dirty || !!parseErr || saving || !profile}
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>

      {parseErr ? <div className="banner danger">JSON parse error: {parseErr}</div> : null}
      {saveErr ? <div className="banner danger">{saveErr}</div> : null}
      {dirty && !parseErr && !saveErr ? (
        <div className="profiles-busy muted">Unsaved changes.</div>
      ) : null}

      <textarea
        className="profiles-textarea mono profiles-json-editor"
        value={draft}
        onChange={onChange}
        spellCheck={false}
        disabled={disabled}
        rows={20}
        placeholder={profile ? "" : "Select a profile to edit JSON."}
      />
    </section>
  );
}

function FieldTable({ title, scope, rows, onSet, onDelete, onPromote }) {
  const [k, setK] = useState("");
  const [v, setV] = useState("");

  return (
    <section className="profiles-card">
      <div className="profiles-card-head">
        <div className="profiles-card-title">{title}</div>
        <div className="profiles-card-sub muted">{Object.keys(rows).length ? `${Object.keys(rows).length} field(s)` : "No fields"}</div>
      </div>

      <div className="profiles-add-row">
        <input
          className="profiles-input mono"
          type="text"
          value={k}
          onChange={(e) => setK(e.target.value)}
          placeholder="key (e.g. application.city)"
          spellCheck={false}
        />
        <input
          className="profiles-input"
          type="text"
          value={v}
          onChange={(e) => setV(e.target.value)}
          placeholder="value"
        />
        <button
          type="button"
          className="btn primary btn--small"
          onClick={() => {
            const key = k.trim();
            if (!key) return;
            onSet?.(key, v);
            setK("");
            setV("");
          }}
        >
          Set
        </button>
      </div>

      {!Object.keys(rows).length ? (
        <div className="profiles-empty muted">Nothing here yet.</div>
      ) : (
        <div className="profiles-table">
          <div className="profiles-row profiles-row--head">
            <div>Key</div>
            <div>Value</div>
            <div />
          </div>
          {Object.entries(rows)
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([key, value]) => (
              <div key={key} className="profiles-row">
                <div className="profiles-k mono" title={key}>
                  {key}
                </div>
                <div className="profiles-v">
                  <input
                    className="profiles-table-input"
                    type="text"
                    value={value ?? ""}
                    onChange={(e) => onSet?.(key, e.target.value)}
                    placeholder="value"
                  />
                </div>
                <div className="profiles-actions">
                  {scope === "relative" ? (
                    <button type="button" className="btn ghost btn--small" onClick={() => onPromote?.(key)} title="Move to absolute_fields">
                      Promote
                    </button>
                  ) : null}
                  <button type="button" className="btn ghost btn--small" onClick={() => onDelete?.(key)} title="Delete field">
                    Delete
                  </button>
                </div>
              </div>
            ))}
        </div>
      )}
    </section>
  );
}

export function ProfilesPage() {
  const [profiles, setProfiles] = useState([]);
  const [selected, setSelected] = useState("");
  const [profile, setProfile] = useState(null);
  const [loadingList, setLoadingList] = useState(false);
  const [loadingProfile, setLoadingProfile] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const [createOpen, setCreateOpen] = useState(false);
  const [createTab, setCreateTab] = useState("blank"); // blank | import
  const [createDraft, setCreateDraft] = useState({ profile_id: "", label: "", profile_type: "" });
  const [importText, setImportText] = useState("");
  const [createErr, setCreateErr] = useState("");
  const [createBusy, setCreateBusy] = useState(false);

  // Bumping this counter forces the AttachmentsCard to re-fetch its rows. We
  // bump it after the user saves the JSON editor (which can mutate the
  // attachments map) so the table stays in sync.
  const [attachmentsRefreshKey, setAttachmentsRefreshKey] = useState(0);

  const { rel, absf } = useMemo(() => getCustomMaps(profile), [profile]);

  const refreshList = async (keepSelection = true) => {
    setLoadingList(true);
    setErr("");
    try {
      const data = await fetchJson(`/api/profiles`);
      const rows = Array.isArray(data?.profiles) ? data.profiles : [];
      setProfiles(rows);
      if (!keepSelection) setSelected("");
    } catch (e) {
      setErr(e?.message || String(e));
    } finally {
      setLoadingList(false);
    }
  };

  const loadProfile = async (pid) => {
    const id = (pid || "").trim();
    if (!id) return;
    setLoadingProfile(true);
    setErr("");
    try {
      const data = await fetchJson(`/api/profiles/${encodeURIComponent(id)}`);
      setProfile(data?.profile || null);
    } catch (e) {
      setProfile(null);
      setErr(e?.message || String(e));
    } finally {
      setLoadingProfile(false);
    }
  };

  useEffect(() => {
    refreshList();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!selected) {
      setProfile(null);
      return;
    }
    loadProfile(selected);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected]);

  const doCustomOp = async (payload) => {
    const pid = (selected || "").trim();
    if (!pid) return;
    setBusy(true);
    setErr("");
    try {
      const data = await fetchJson(`/api/profiles/${encodeURIComponent(pid)}/custom-fields`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      setProfile(data?.profile || profile);
    } catch (e) {
      setErr(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  const createProfile = async () => {
    setCreateErr("");
    setCreateTab("blank");
    setCreateDraft({ profile_id: "", label: "", profile_type: "" });
    setImportText("");
    setCreateOpen(true);
  };

  const submitCreate = async () => {
    setCreateErr("");
    const pid = String(createDraft.profile_id || "").trim();
    if (!pid) {
      setCreateErr("Profile ID is required.");
      return;
    }

    let prof = null;
    if (createTab === "import") {
      try {
        const parsed = JSON.parse(importText || "");
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("JSON must be an object");
        prof = { ...parsed };
      } catch (e) {
        setCreateErr(`Invalid JSON: ${e?.message || String(e)}`);
        return;
      }
    } else {
      const nowIso = new Date().toISOString();
      prof = {
        schema_version: "1.0",
        profile_id: pid,
        profile_type: String(createDraft.profile_type || "").trim() || "general",
        label: String(createDraft.label || "").trim() || pid,
        base: {},
        other: { custom: { relative_fields: {}, absolute_fields: {} } },
        attachments: {},
        source_pdf_path: null,
        created_at: nowIso,
        updated_at: nowIso,
      };
    }

    // Force ids/metadata from the modal.
    prof.profile_id = pid;
    if (createTab !== "import") {
      // already set
    } else {
      if (createDraft.label && typeof prof.label !== "string") prof.label = String(createDraft.label).trim();
      if (createDraft.profile_type && typeof prof.profile_type !== "string") prof.profile_type = String(createDraft.profile_type).trim();
      if (!prof.label) prof.label = String(createDraft.label || "").trim() || pid;
      if (!prof.profile_type) prof.profile_type = String(createDraft.profile_type || "").trim() || "general";
    }

    setCreateBusy(true);
    try {
      await fetchJson(`/api/profiles/${encodeURIComponent(pid)}`, {
        method: "PUT",
        body: JSON.stringify({ profile: prof }),
      });
      setCreateOpen(false);
      await refreshList();
      setSelected(pid);
    } catch (e) {
      setCreateErr(e?.message || String(e));
    } finally {
      setCreateBusy(false);
    }
  };

  const deleteSelectedProfile = async () => {
    const pid = (selected || "").trim();
    if (!pid) return;
    if (!confirm(`Delete profile "${pid}"? This cannot be undone.`)) return;
    setBusy(true);
    setErr("");
    try {
      await fetchJson(`/api/profiles/${encodeURIComponent(pid)}`, { method: "DELETE" });
      setSelected("");
      setProfile(null);
      await refreshList(false);
    } catch (e) {
      setErr(e?.message || String(e));
    } finally {
      setBusy(false);
    }
  };

  const listEmpty = !profiles.length && !loadingList;

  const createModal = createOpen ? (
    <div
      className="modal-backdrop"
      role="presentation"
      onClick={(ev) => {
        if (ev.target === ev.currentTarget && !createBusy) setCreateOpen(false);
      }}
    >
      <div className="modal-dialog profiles-modal" role="dialog" aria-modal="true" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2>New profile</h2>
          <button type="button" className="modal-close" onClick={() => !createBusy && setCreateOpen(false)} aria-label="Close">
            ×
          </button>
        </div>
        <div className="profiles-modal-body">
          {createErr ? <div className="banner danger">{createErr}</div> : null}
          <div className="profiles-tabbar" role="tablist" aria-label="Create profile mode">
            <button
              type="button"
              className={`btn nav-btn ${createTab === "blank" ? "btn--active" : ""}`}
              onClick={() => setCreateTab("blank")}
              role="tab"
              aria-selected={createTab === "blank"}
            >
              Blank
            </button>
            <button
              type="button"
              className={`btn nav-btn ${createTab === "import" ? "btn--active" : ""}`}
              onClick={() => setCreateTab("import")}
              role="tab"
              aria-selected={createTab === "import"}
            >
              Import JSON
            </button>
          </div>

          <div className="profiles-modal-grid">
            <label className="profiles-field">
              <span>Profile ID</span>
              <input
                className="profiles-input mono"
                type="text"
                value={createDraft.profile_id}
                onChange={(e) => setCreateDraft((d) => ({ ...d, profile_id: e.target.value }))}
                placeholder="e.g. main"
                spellCheck={false}
                autoFocus
              />
            </label>
            <label className="profiles-field">
              <span>Label (optional)</span>
              <input
                className="profiles-input"
                type="text"
                value={createDraft.label}
                onChange={(e) => setCreateDraft((d) => ({ ...d, label: e.target.value }))}
                placeholder="Human-friendly name"
              />
            </label>
            <label className="profiles-field">
              <span>Profile type (optional)</span>
              <input
                className="profiles-input mono"
                type="text"
                value={createDraft.profile_type}
                onChange={(e) => setCreateDraft((d) => ({ ...d, profile_type: e.target.value }))}
                placeholder="e.g. software_engineer"
                spellCheck={false}
              />
            </label>
          </div>

          {createTab === "import" ? (
            <div className="profiles-import">
              <div className="profiles-import-row">
                <label className="profiles-field" style={{ marginTop: 0 }}>
                  <span>Import file (.json)</span>
                  <input
                    className="profiles-file"
                    type="file"
                    accept="application/json,.json"
                    onChange={async (e) => {
                      const f = e.target.files?.[0];
                      if (!f) return;
                      try {
                        const txt = await readFileAsText(f);
                        setImportText(txt);
                      } catch (ex) {
                        setCreateErr(ex?.message || String(ex));
                      }
                    }}
                    disabled={createBusy}
                  />
                </label>
                <button
                  type="button"
                  className="btn ghost btn--small"
                  onClick={() => setImportText("")}
                  disabled={createBusy || !importText}
                  title="Clear import JSON"
                >
                  Clear
                </button>
              </div>
              <label className="profiles-field">
                <span>JSON</span>
                <textarea
                  className="profiles-textarea mono"
                  rows={10}
                  value={importText}
                  onChange={(e) => setImportText(e.target.value)}
                  placeholder='Paste a full profile object JSON here (must be an object).'
                  spellCheck={false}
                />
              </label>
            </div>
          ) : null}

          <div className="modal-actions">
            <button type="button" className="btn ghost" onClick={() => setCreateOpen(false)} disabled={createBusy}>
              Cancel
            </button>
            <button type="button" className="btn primary" onClick={submitCreate} disabled={createBusy}>
              {createBusy ? "Saving…" : createTab === "import" ? "Import" : "Create"}
            </button>
          </div>
        </div>
      </div>
    </div>
  ) : null;

  return (
    <div className="profiles-page">
      {createModal}
      <div className="profiles-head">
        <div>
          <div className="profiles-title">Profiles</div>
          <div className="profiles-sub muted">Manage `profiles_db.json` (list, add, edit, and promote relative → absolute fields).</div>
        </div>
        <div className="profiles-head-actions">
          <button type="button" className="btn ghost" onClick={() => refreshList()} disabled={loadingList}>
            {loadingList ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      {err ? <div className="banner danger">{err}</div> : null}

      <section className="profiles-card profiles-card--wide">
        <div className="profiles-form-row">
          <label className="profiles-field">
            <span>Profile</span>
            <select
              className="profiles-input"
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
              aria-label="Select profile"
            >
              <option value="">{loadingList ? "Loading…" : "Select…"}</option>
              {profiles.map((p) => (
                <option key={p.profile_id} value={p.profile_id}>
                  {p.profile_id}
                  {p.label ? ` — ${p.label}` : ""}
                </option>
              ))}
            </select>
          </label>
          <div className="profiles-inline-actions">
            <button type="button" className="btn primary" onClick={createProfile} disabled={busy}>
              + New profile
            </button>
            <button type="button" className="btn ghost" onClick={() => refreshList()} disabled={busy || loadingList}>
              Sync list
            </button>
            <button type="button" className="btn ghost" onClick={deleteSelectedProfile} disabled={busy || !selected}>
              Delete profile
            </button>
          </div>
        </div>

        {listEmpty ? <div className="profiles-empty muted">No profiles found for this applicant.</div> : null}
      </section>

      {!selected ? (
        <div className="profiles-empty muted">Select a profile to edit its fields.</div>
      ) : loadingProfile ? (
        <div className="profiles-empty muted">Loading profile…</div>
      ) : !profile ? (
        <div className="profiles-empty muted">Profile not loaded.</div>
      ) : (
        <div className="profiles-grid">
          <FieldTable
            title="Relative fields"
            scope="relative"
            rows={rel}
            onSet={(key, value) => doCustomOp({ op: "set", scope: "relative", key, value })}
            onDelete={(key) => doCustomOp({ op: "delete", scope: "relative", key })}
            onPromote={(key) => doCustomOp({ op: "promote", key })}
          />
          <FieldTable
            title="Absolute fields"
            scope="absolute"
            rows={absf}
            onSet={(key, value) => doCustomOp({ op: "set", scope: "absolute", key, value })}
            onDelete={(key) => doCustomOp({ op: "delete", scope: "absolute", key })}
          />

          <AttachmentsCard
            profileId={selected}
            busy={busy}
            refreshKey={attachmentsRefreshKey}
            onError={(msg) => setErr(msg)}
            onProfileChanged={() => loadProfile(selected)}
          />

          <JsonEditorCard
            profile={profile}
            profileId={selected}
            busy={busy}
            refreshing={loadingProfile}
            onRefresh={() => loadProfile(selected)}
            onProfileSaved={(p) => {
              setProfile(p);
              setAttachmentsRefreshKey((n) => n + 1);
            }}
          />
        </div>
      )}

      {busy ? <div className="profiles-busy muted">Saving…</div> : null}
    </div>
  );
}

