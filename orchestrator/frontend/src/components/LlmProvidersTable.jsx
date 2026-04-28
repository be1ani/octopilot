import { Fragment, useCallback, useEffect, useMemo, useState } from "react";

/**
 * Table of LLM providers configured on the orchestrator backend.
 *
 * Empty by default — the user adds providers via the "Add LLM" button. The
 * server is the single source of truth: the API key for any saved provider is
 * never returned in plaintext, only as a masked preview. To replace a key the
 * user re-enters it in the edit dialog.
 *
 * Each row expands to show read-only per-model pricing for that platform,
 * sourced from agent/pricing.json on the host. The "Spend" column is the sum
 * of cost_usd from the JSONL ledger grouped by provider.
 */
function fmtUsd(n, digits = 2) {
  if (typeof n !== "number" || !Number.isFinite(n)) return "—";
  return `$${n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

function fmtPrice1m(n) {
  if (typeof n !== "number" || !Number.isFinite(n)) return "—";
  if (n === 0) return "$0";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  return `$${n.toFixed(2)}`;
}

function ProviderForm({ available, initial, onCancel, onSave, busy }) {
  const [provider, setProvider] = useState(initial?.provider || available?.[0]?.id || "");
  const [apiKey, setApiKey] = useState("");
  const [show, setShow] = useState(false);
  const editing = Boolean(initial?.provider);

  const meta = useMemo(
    () => available?.find((p) => p.id === provider) || null,
    [available, provider]
  );

  return (
    <div className="llmp-form">
      <div className="llmp-form-row">
        <label className="settings-field" style={{ marginTop: 0, flex: "1 1 220px" }}>
          <span>Platform</span>
          <select
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            disabled={editing || busy}
          >
            {available.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
        </label>
        <label className="settings-field" style={{ marginTop: 0, flex: "2 1 320px" }}>
          <span>API key {meta?.env_var ? <span className="muted">({meta.env_var})</span> : null}</span>
          <div className="settings-input-row">
            <input
              type={show ? "text" : "password"}
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={editing ? "Re-enter key to replace" : "sk-…"}
              autoComplete="off"
              spellCheck={false}
            />
            <button
              type="button"
              className="btn ghost btn--small"
              onClick={() => setShow((v) => !v)}
            >
              {show ? "Hide" : "Show"}
            </button>
          </div>
        </label>
      </div>
      <div className="llmp-form-actions">
        <button type="button" className="btn ghost btn--small" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button
          type="button"
          className="btn primary btn--small"
          onClick={() => onSave({ provider, api_key: apiKey })}
          disabled={busy || !provider || !apiKey.trim()}
        >
          {busy ? "Saving…" : editing ? "Replace key" : "Save"}
        </button>
      </div>
    </div>
  );
}

function PricingTable({ models }) {
  if (!models?.length) {
    return (
      <div className="muted llmp-empty-models">
        No pricing entries for this platform in <span className="mono">agent/pricing.json</span>.
      </div>
    );
  }
  return (
    <div className="llmp-models-wrap">
      <table className="llmp-models mono">
        <thead>
          <tr>
            <th style={{ textAlign: "left" }}>Model</th>
            <th style={{ textAlign: "right" }}>$/1M input</th>
            <th style={{ textAlign: "right" }}>$/1M output</th>
            <th style={{ textAlign: "right" }}>$/1M cached</th>
          </tr>
        </thead>
        <tbody>
          {models.map((m) => (
            <tr key={m.id}>
              <td>{m.id}</td>
              <td style={{ textAlign: "right" }}>{fmtPrice1m(m.usd_per_1m_input)}</td>
              <td style={{ textAlign: "right" }}>{fmtPrice1m(m.usd_per_1m_output)}</td>
              <td style={{ textAlign: "right" }}>
                {typeof m.usd_per_1m_cached_input === "number" ? fmtPrice1m(m.usd_per_1m_cached_input) : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function LlmProvidersTable() {
  const [providers, setProviders] = useState([]);
  const [available, setAvailable] = useState([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [adding, setAdding] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [busyRow, setBusyRow] = useState(null);
  const [expanded, setExpanded] = useState(() => new Set());

  const reload = useCallback(async () => {
    setLoading(true);
    setErr("");
    try {
      const res = await fetch("/api/llm-providers");
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.error || res.statusText || "Failed");
      setProviders(Array.isArray(data?.providers) ? data.providers : []);
      setAvailable(Array.isArray(data?.available) ? data.available : []);
    } catch (e) {
      setErr(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const addable = useMemo(() => {
    const configured = new Set(providers.map((p) => p.provider));
    return available.filter((p) => !configured.has(p.id));
  }, [available, providers]);

  const onSave = async ({ provider, api_key }) => {
    setBusyRow(provider);
    setErr("");
    try {
      const res = await fetch("/api/llm-providers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, api_key }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.error || res.statusText || "Failed");
      setAdding(false);
      setEditingId(null);
      await reload();
    } catch (e) {
      setErr(e?.message || String(e));
    } finally {
      setBusyRow(null);
    }
  };

  const onDelete = async (provider) => {
    if (!window.confirm(`Remove the stored API key for "${provider}"?`)) return;
    setBusyRow(provider);
    setErr("");
    try {
      const res = await fetch(`/api/llm-providers/${encodeURIComponent(provider)}`, {
        method: "DELETE",
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.error || res.statusText || "Failed");
      await reload();
    } catch (e) {
      setErr(e?.message || String(e));
    } finally {
      setBusyRow(null);
    }
  };

  const toggleExpand = (pid) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(pid)) next.delete(pid);
      else next.add(pid);
      return next;
    });
  };

  return (
    <section className="settings-card settings-card--wide">
      <div className="settings-card-title">LLM providers</div>
      <div className="settings-card-sub muted">
        API keys for each platform are stored in MongoDB on the orchestrator and injected into
        every agent container as environment variables. Saved keys are never returned in plaintext —
        to change one, replace it.
      </div>

      <div className="settings-inline-actions" style={{ marginTop: "0.65rem" }}>
        <button
          type="button"
          className="btn primary btn--small"
          onClick={() => {
            setEditingId(null);
            setAdding(true);
          }}
          disabled={loading || adding || !addable.length}
          title={!addable.length ? "All known platforms are already configured" : undefined}
        >
          + Add LLM
        </button>
        <button
          type="button"
          className="btn ghost btn--small"
          onClick={reload}
          disabled={loading}
        >
          {loading ? "Loading…" : "Refresh"}
        </button>
        {err ? <span className="muted" style={{ color: "#fecaca" }}>{err}</span> : null}
      </div>

      {adding ? (
        <div className="llmp-add-card">
          <ProviderForm
            available={addable}
            initial={null}
            busy={busyRow != null}
            onCancel={() => setAdding(false)}
            onSave={onSave}
          />
        </div>
      ) : null}

      {!providers.length && !adding ? (
        <div className="llmp-empty">
          <div className="llmp-empty-title">No LLM platforms configured yet.</div>
          <div className="muted llmp-empty-sub">
            Click <strong>+ Add LLM</strong> to plug in a provider. Agents will fail to start until at
            least one platform key is stored.
          </div>
        </div>
      ) : null}

      {providers.length ? (
        <div className="settings-table-wrap" style={{ marginTop: "0.85rem" }}>
          <table className="settings-table" aria-label="LLM providers">
            <thead>
              <tr>
                <th style={{ width: 36 }}></th>
                <th>Platform</th>
                <th>Env var</th>
                <th>API key</th>
                <th style={{ textAlign: "right", width: 130 }}>Spend (ledger)</th>
                <th style={{ textAlign: "right", width: 220 }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {providers.map((p) => {
                const isOpen = expanded.has(p.provider);
                const isEditing = editingId === p.provider;
                return (
                  <Fragment key={p.provider}>
                    <tr>
                      <td>
                        <button
                          type="button"
                          className="llmp-expand-btn"
                          onClick={() => toggleExpand(p.provider)}
                          aria-label={isOpen ? "Collapse" : "Expand"}
                        >
                          {isOpen ? "▾" : "▸"}
                        </button>
                      </td>
                      <td>
                        <div className="llmp-platform">
                          <strong>{p.label || p.provider}</strong>
                          <div className="muted llmp-platform-id">{p.provider}</div>
                        </div>
                      </td>
                      <td className="mono">{p.env_var || "—"}</td>
                      <td className="mono">{p.key_masked || "—"}</td>
                      <td style={{ textAlign: "right" }} className="mono">
                        {fmtUsd(p.spend_usd, p.spend_usd > 1 ? 2 : 4)}
                      </td>
                      <td style={{ textAlign: "right" }}>
                        <button
                          type="button"
                          className="btn ghost btn--small"
                          onClick={() => {
                            setAdding(false);
                            setEditingId(isEditing ? null : p.provider);
                          }}
                          disabled={busyRow === p.provider}
                        >
                          {isEditing ? "Cancel" : "Edit"}
                        </button>{" "}
                        <button
                          type="button"
                          className="btn ghost btn--small llmp-danger"
                          onClick={() => onDelete(p.provider)}
                          disabled={busyRow === p.provider}
                        >
                          Delete
                        </button>
                      </td>
                    </tr>
                    {isEditing ? (
                      <tr>
                        <td></td>
                        <td colSpan={5}>
                          <ProviderForm
                            available={[{ id: p.provider, label: p.label, env_var: p.env_var }]}
                            initial={{ provider: p.provider }}
                            busy={busyRow === p.provider}
                            onCancel={() => setEditingId(null)}
                            onSave={onSave}
                          />
                        </td>
                      </tr>
                    ) : null}
                    {isOpen ? (
                      <tr className="llmp-models-row">
                        <td></td>
                        <td colSpan={5}>
                          <div className="llmp-models-title">Per-model pricing</div>
                          <PricingTable models={p.models} />
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}
