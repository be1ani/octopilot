import { useEffect, useMemo, useState } from "react";
import { clearLlmKeys, readLlmKeys, writeLlmKeys } from "../llmKeys.js";
import "./SettingsPage.css";

function maskKey(k) {
  const s = String(k || "");
  if (!s) return "";
  if (s.length <= 10) return "•".repeat(Math.max(6, s.length));
  return `${s.slice(0, 4)}…${s.slice(-4)}`;
}

function envLine(name, value) {
  const v = String(value || "").trim();
  if (!v) return "";
  // Minimal escaping: wrap in quotes if whitespace present.
  const needsQuotes = /\s/.test(v);
  const safe = needsQuotes ? `"${v.replaceAll('"', '\\"')}"` : v;
  return `${name}=${safe}`;
}

export function SettingsPage() {
  const [draft, setDraft] = useState(() => readLlmKeys());
  const [show, setShow] = useState({ openai: false, anthropic: false, google: false });
  const [savedAt, setSavedAt] = useState(0);
  const [copied, setCopied] = useState(false);
  const [serverDraft, setServerDraft] = useState(null);
  const [serverBusy, setServerBusy] = useState(false);
  const [serverErr, setServerErr] = useState("");
  const [reconcileOut, setReconcileOut] = useState("");
  const [reconcileBusy, setReconcileBusy] = useState(false);
  const [reconcileStart, setReconcileStart] = useState(() => new Date(Date.now() - 7 * 86400000).toISOString().slice(0, 10));
  const [reconcileEnd, setReconcileEnd] = useState(() => new Date().toISOString().slice(0, 10));
  const [ledgerRows, setLedgerRows] = useState([]);
  const [ledgerBusy, setLedgerBusy] = useState(false);
  const [ledgerErr, setLedgerErr] = useState("");
  const [ledgerLimit, setLedgerLimit] = useState(200);
  const [ledgerEdits, setLedgerEdits] = useState({});
  const [ledgerSaveBusy, setLedgerSaveBusy] = useState(false);

  const envText = useMemo(() => {
    const lines = [
      envLine("OPENAI_API_KEY", draft.openai),
      envLine("OPENAI_ADMIN_KEY", draft.openai_admin),
      envLine("ANTHROPIC_API_KEY", draft.anthropic),
      envLine("GOOGLE_API_KEY", draft.google),
    ].filter(Boolean);
    return lines.length ? `${lines.join("\n")}\n` : "";
  }, [draft]);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      setServerErr("");
      setServerBusy(true);
      try {
        const res = await fetch("/api/settings");
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data?.error || res.statusText || "Failed");
        if (!alive) return;
        setServerDraft(data?.settings || {});
      } catch (e) {
        if (!alive) return;
        setServerErr(e?.message || String(e));
      } finally {
        if (alive) setServerBusy(false);
      }
    };
    load();
    return () => {
      alive = false;
    };
  }, []);

  const onSave = () => {
    writeLlmKeys(draft);
    setSavedAt(Date.now());
  };

  const onReset = () => {
    setDraft(readLlmKeys());
  };

  const onClear = () => {
    clearLlmKeys();
    setDraft({ openai: "", openai_admin: "", anthropic: "", google: "" });
    setSavedAt(Date.now());
  };

  const onCopyEnv = async () => {
    if (!envText) return;
    try {
      await navigator.clipboard.writeText(envText);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch {
      // ignore: clipboard might be blocked; user can manually copy
    }
  };

  return (
    <div className="settings-page">
      <div className="settings-head">
        <div>
          <div className="settings-title">Settings</div>
          <div className="settings-sub muted">
            Configure provider API keys locally in your browser. These values are stored in <span className="mono">localStorage</span>.
          </div>
        </div>
        <div className="settings-actions">
          <button type="button" className="btn ghost" onClick={onReset}>
            Reset
          </button>
          <button type="button" className="btn ghost" onClick={onClear}>
            Clear
          </button>
          <button type="button" className="btn primary" onClick={onSave}>
            Save
          </button>
        </div>
      </div>

      {savedAt ? (
        <div className="banner warn" style={{ marginTop: "0.75rem" }}>
          Saved. (Local only) Current values:{" "}
          <span className="mono">
            OpenAI {draft.openai ? maskKey(draft.openai) : "—"} · Claude {draft.anthropic ? maskKey(draft.anthropic) : "—"} · Google{" "}
            {draft.google ? maskKey(draft.google) : "—"}
          </span>
        </div>
      ) : null}

      <div className="settings-grid">
        <section className="settings-card">
          <div className="settings-card-title">OpenAI</div>
          <label className="settings-field">
            <span>API key</span>
            <div className="settings-input-row">
              <input
                type={show.openai ? "text" : "password"}
                value={draft.openai}
                onChange={(e) => setDraft((d) => ({ ...d, openai: e.target.value }))}
                placeholder="sk-…"
                autoComplete="off"
                spellCheck={false}
              />
              <button type="button" className="btn ghost btn--small" onClick={() => setShow((s) => ({ ...s, openai: !s.openai }))}>
                {show.openai ? "Hide" : "Show"}
              </button>
            </div>
          </label>
          <label className="settings-field">
            <span>Admin key (usage/costs reconciliation)</span>
            <div className="settings-input-row">
              <input
                type="password"
                value={draft.openai_admin || ""}
                onChange={(e) => setDraft((d) => ({ ...d, openai_admin: e.target.value }))}
                placeholder="sk-admin-…"
                autoComplete="off"
                spellCheck={false}
              />
            </div>
          </label>
        </section>

        <section className="settings-card">
          <div className="settings-card-title">Claude (Anthropic)</div>
          <label className="settings-field">
            <span>API key</span>
            <div className="settings-input-row">
              <input
                type={show.anthropic ? "text" : "password"}
                value={draft.anthropic}
                onChange={(e) => setDraft((d) => ({ ...d, anthropic: e.target.value }))}
                placeholder="sk-ant-…"
                autoComplete="off"
                spellCheck={false}
              />
              <button
                type="button"
                className="btn ghost btn--small"
                onClick={() => setShow((s) => ({ ...s, anthropic: !s.anthropic }))}
              >
                {show.anthropic ? "Hide" : "Show"}
              </button>
            </div>
          </label>
        </section>

        <section className="settings-card">
          <div className="settings-card-title">Google (Gemini)</div>
          <label className="settings-field">
            <span>API key</span>
            <div className="settings-input-row">
              <input
                type={show.google ? "text" : "password"}
                value={draft.google}
                onChange={(e) => setDraft((d) => ({ ...d, google: e.target.value }))}
                placeholder="AIza…"
                autoComplete="off"
                spellCheck={false}
              />
              <button type="button" className="btn ghost btn--small" onClick={() => setShow((s) => ({ ...s, google: !s.google }))}>
                {show.google ? "Hide" : "Show"}
              </button>
            </div>
          </label>
        </section>

        <section className="settings-card settings-card--wide">
          <div className="settings-card-title">Copy as .env</div>
          <div className="settings-card-sub muted">
            If you want the agent containers to use these keys, paste this into your repo’s <span className="mono">.env</span> (mounted into the agent).
          </div>
          <div className="settings-env">
            <pre className="settings-pre">{envText || "# (no keys set)\n"}</pre>
            <div className="settings-env-actions">
              <button type="button" className="btn ghost btn--small" onClick={onCopyEnv} disabled={!envText}>
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
          </div>
        </section>

        <section className="settings-card settings-card--wide">
          <div className="settings-card-title">Orchestrator limits & budget</div>
          <div className="settings-card-sub muted">
            These are stored on the orchestrator backend (<span className="mono">/api/settings</span>) and enforced for both auto-start and manual start.
          </div>

          <div className="settings-two-col">
            <label className="settings-field">
              <span>Max parallel machines</span>
              <input
                type="number"
                min="0"
                step="1"
                value={serverDraft?.max_parallel_machines ?? ""}
                onChange={(e) =>
                  setServerDraft((s) => ({ ...(s || {}), max_parallel_machines: e.target.value === "" ? "" : Number(e.target.value) }))
                }
                placeholder="e.g. 4"
              />
            </label>
            <label className="settings-field">
              <span>Global budget alert (USD)</span>
              <input
                type="number"
                min="0"
                step="0.01"
                value={serverDraft?.budget_alert_usd ?? ""}
                onChange={(e) =>
                  setServerDraft((s) => ({ ...(s || {}), budget_alert_usd: e.target.value === "" ? "" : Number(e.target.value) }))
                }
                placeholder="e.g. 25"
              />
            </label>
            <label className="settings-field">
              <span>Max budget per machine (USD, LLM only)</span>
              <input
                type="number"
                min="0"
                step="0.01"
                value={serverDraft?.max_budget_per_machine_usd ?? ""}
                onChange={(e) =>
                  setServerDraft((s) => ({
                    ...(s || {}),
                    max_budget_per_machine_usd: e.target.value === "" ? "" : Number(e.target.value),
                  }))
                }
                placeholder="e.g. 1.50"
              />
            </label>
            <label className="settings-field">
              <span>Ledger path (relative)</span>
              <input
                type="text"
                value={serverDraft?.llm_ledger_relpath ?? ""}
                onChange={(e) => setServerDraft((s) => ({ ...(s || {}), llm_ledger_relpath: e.target.value }))}
                placeholder="orchestrator/data/llm_ledger.jsonl"
                spellCheck={false}
              />
            </label>
          </div>

          <div className="settings-inline-actions">
            <button
              type="button"
              className="btn ghost btn--small"
              onClick={async () => {
                setServerErr("");
                setServerBusy(true);
                try {
                  const res = await fetch("/api/settings");
                  const data = await res.json().catch(() => ({}));
                  if (!res.ok) throw new Error(data?.error || res.statusText || "Failed");
                  setServerDraft(data?.settings || {});
                } catch (e) {
                  setServerErr(e?.message || String(e));
                } finally {
                  setServerBusy(false);
                }
              }}
              disabled={serverBusy}
            >
              {serverDraft ? "Reload" : "Load"}
            </button>
            <button
              type="button"
              className="btn primary btn--small"
              onClick={async () => {
                if (!serverDraft) return;
                setServerErr("");
                setServerBusy(true);
                try {
                  const res = await fetch("/api/settings", {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(serverDraft),
                  });
                  const data = await res.json().catch(() => ({}));
                  if (!res.ok) throw new Error(data?.error || res.statusText || "Failed");
                  setServerDraft(data?.settings || serverDraft);
                } catch (e) {
                  setServerErr(e?.message || String(e));
                } finally {
                  setServerBusy(false);
                }
              }}
              disabled={serverBusy || !serverDraft}
            >
              Save backend settings
            </button>
            {serverErr ? <span className="muted" style={{ color: "#fecaca" }}>{serverErr}</span> : null}
          </div>
        </section>

        <section className="settings-card settings-card--wide">
          <div className="settings-card-title">Model pricing overrides (tokens → dollars)</div>
          <div className="settings-card-sub muted">
            Paste an overrides object that will be injected into agent containers as <span className="mono">AGENT_LLM_PRICING_JSON</span>.
            Expected shape: <span className="mono">{`{ "models": { "gpt-4.1": { "usd_per_1m_input": 2, "usd_per_1m_output": 8 } } }`}</span>
          </div>

          <label className="settings-field">
            <span>Overrides JSON</span>
            <textarea
              className="settings-textarea mono"
              rows={10}
              value={serverDraft?.llm_pricing_overrides ? JSON.stringify(serverDraft.llm_pricing_overrides, null, 2) : ""}
              onChange={(e) => {
                const raw = e.target.value;
                if (!serverDraft) return;
                if (!raw.trim()) {
                  setServerDraft((s) => ({ ...(s || {}), llm_pricing_overrides: null }));
                  return;
                }
                try {
                  const obj = JSON.parse(raw);
                  setServerDraft((s) => ({ ...(s || {}), llm_pricing_overrides: obj }));
                  setServerErr("");
                } catch (err) {
                  setServerErr("Invalid JSON in pricing overrides.");
                }
              }}
              placeholder='{"models":{"gpt-4.1":{"usd_per_1m_input":2,"usd_per_1m_output":8}}}'
              spellCheck={false}
            />
          </label>
        </section>

        <section className="settings-card settings-card--wide">
          <div className="settings-card-title">Reconcile OpenAI usage</div>
          <div className="settings-card-sub muted">
            Runs <span className="mono">python -m agent.reconcile_openai_usage</span> on the orchestrator host. Requires the OpenAI <em>admin</em> key.
          </div>

          <div className="settings-two-col">
            <label className="settings-field">
              <span>Start (YYYY-MM-DD)</span>
              <input type="text" value={reconcileStart} onChange={(e) => setReconcileStart(e.target.value)} className="mono" />
            </label>
            <label className="settings-field">
              <span>End (YYYY-MM-DD)</span>
              <input type="text" value={reconcileEnd} onChange={(e) => setReconcileEnd(e.target.value)} className="mono" />
            </label>
          </div>

          <div className="settings-inline-actions">
            <button
              type="button"
              className="btn primary btn--small"
              onClick={async () => {
                setReconcileBusy(true);
                setReconcileOut("");
                try {
                  const hdrs = { "Content-Type": "application/json" };
                  // Send admin key via header (stored locally).
                  if (draft?.openai_admin) hdrs["X-OpenAI-Admin-Key"] = draft.openai_admin;
                  const res = await fetch("/api/reconcile/openai", {
                    method: "POST",
                    headers: hdrs,
                    body: JSON.stringify({
                      start: reconcileStart,
                      end: reconcileEnd,
                      ledger_relpath: serverDraft?.llm_ledger_relpath || undefined,
                    }),
                  });
                  const data = await res.json().catch(() => ({}));
                  if (!res.ok) throw new Error(data?.error || res.statusText || "Reconcile failed");
                  setReconcileOut(data?.stdout || "(no output)");
                } catch (e) {
                  setReconcileOut(`ERROR: ${e?.message || String(e)}`);
                } finally {
                  setReconcileBusy(false);
                }
              }}
              disabled={reconcileBusy}
              title="Runs on the orchestrator host"
            >
              {reconcileBusy ? "Running…" : "Run reconcile"}
            </button>
          </div>

          {reconcileOut ? <pre className="settings-pre" style={{ marginTop: "0.6rem" }}>{reconcileOut}</pre> : null}
        </section>

        <section className="settings-card settings-card--wide">
          <div className="settings-card-title">LLM ledger (tail)</div>
          <div className="settings-card-sub muted">
            Live view of the append-only JSONL ledger. You can save per-request overrides (stored in orchestrator state) for auditing/adjustments.
          </div>

          <div className="settings-inline-actions">
            <label className="settings-field" style={{ marginTop: 0, maxWidth: 220 }}>
              <span>Rows</span>
              <input
                type="number"
                min="1"
                max="2000"
                step="1"
                value={ledgerLimit}
                onChange={(e) => setLedgerLimit(Math.max(1, Math.min(2000, Number(e.target.value || 200))))}
              />
            </label>
            <button
              type="button"
              className="btn ghost btn--small"
              onClick={async () => {
                setLedgerErr("");
                setLedgerBusy(true);
                try {
                  const res = await fetch(`/api/ledger?limit=${encodeURIComponent(String(ledgerLimit || 200))}`);
                  const data = await res.json().catch(() => ({}));
                  if (!res.ok) throw new Error(data?.error || res.statusText || "Failed");
                  setLedgerRows(Array.isArray(data?.rows) ? data.rows : []);
                } catch (e) {
                  setLedgerErr(e?.message || String(e));
                } finally {
                  setLedgerBusy(false);
                }
              }}
              disabled={ledgerBusy}
            >
              {ledgerBusy ? "Loading…" : ledgerRows.length ? "Refresh" : "Load"}
            </button>
            <button
              type="button"
              className="btn primary btn--small"
              onClick={async () => {
                setLedgerSaveBusy(true);
                setLedgerErr("");
                try {
                  const res = await fetch("/api/ledger/overrides", {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ overrides: ledgerEdits }),
                  });
                  const data = await res.json().catch(() => ({}));
                  if (!res.ok) throw new Error(data?.error || res.statusText || "Save failed");
                  setLedgerEdits({});
                } catch (e) {
                  setLedgerErr(e?.message || String(e));
                } finally {
                  setLedgerSaveBusy(false);
                }
              }}
              disabled={ledgerSaveBusy || !Object.keys(ledgerEdits).length}
              title="Saves overrides into orchestrator state.json (does not rewrite the ledger file)"
            >
              {ledgerSaveBusy ? "Saving…" : "Save overrides"}
            </button>
            {ledgerErr ? <span className="muted" style={{ color: "#fecaca" }}>{ledgerErr}</span> : null}
          </div>

          {!ledgerRows.length ? (
            <div className="muted" style={{ marginTop: "0.65rem", fontSize: "0.85rem" }}>
              No rows loaded yet.
            </div>
          ) : (
            <div className="settings-table-wrap">
              <table className="settings-table mono" aria-label="LLM ledger table">
                <thead>
                  <tr>
                    <th style={{ width: 210 }}>ts</th>
                    <th style={{ width: 80 }}>prov</th>
                    <th>model</th>
                    <th style={{ width: 70 }}>tier</th>
                    <th style={{ width: 90, textAlign: "right" }}>tokens</th>
                    <th style={{ width: 110, textAlign: "right" }}>cost</th>
                    <th style={{ width: 150 }}>request_id</th>
                    <th style={{ width: 140, textAlign: "right" }}>override cost</th>
                    <th style={{ width: 260 }}>override note</th>
                  </tr>
                </thead>
                <tbody>
                  {ledgerRows.map((r, idx) => {
                    const rid = String(r?.request_id || r?.id || "").trim() || `row:${idx}`;
                    const usage = r?.usage && typeof r.usage === "object" ? r.usage : {};
                    const totalTokens = Number(usage?.total_tokens ?? usage?.total ?? 0) || 0;
                    const cost = r?.cost_usd;
                    const ov = r?.override && typeof r.override === "object" ? r.override : {};
                    const edit = ledgerEdits[rid] || {};
                    const ovCost = edit.cost_usd !== undefined ? edit.cost_usd : ov?.cost_usd;
                    const ovNote = edit.note !== undefined ? edit.note : ov?.note;
                    return (
                      <tr key={rid}>
                        <td className="settings-td-clip" title={String(r?.ts || "")}>{String(r?.ts || "")}</td>
                        <td className="settings-td-clip">{String(r?.provider || "")}</td>
                        <td className="settings-td-clip" title={String(r?.model || "")}>{String(r?.model || "")}</td>
                        <td className="settings-td-clip">{String(r?.tier || "")}</td>
                        <td style={{ textAlign: "right" }}>{Number(totalTokens).toLocaleString()}</td>
                        <td style={{ textAlign: "right" }}>{typeof cost === "number" ? `$${cost.toFixed(6)}` : "—"}</td>
                        <td className="settings-td-clip" title={rid}>{rid}</td>
                        <td style={{ textAlign: "right" }}>
                          <input
                            className="settings-table-input mono"
                            type="number"
                            step="0.000001"
                            value={ovCost ?? ""}
                            onChange={(e) => {
                              const v = e.target.value;
                              setLedgerEdits((p) => ({ ...p, [rid]: { ...(p[rid] || {}), cost_usd: v === "" ? null : Number(v) } }));
                            }}
                          />
                        </td>
                        <td>
                          <input
                            className="settings-table-input"
                            type="text"
                            value={ovNote ?? ""}
                            onChange={(e) =>
                              setLedgerEdits((p) => ({ ...p, [rid]: { ...(p[rid] || {}), note: e.target.value } }))
                            }
                            placeholder="optional note"
                          />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

