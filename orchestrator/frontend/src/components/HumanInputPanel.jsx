import { useCallback, useEffect, useState } from "react";
import "./HumanInputPanel.css";

const ATTENTION_MUTE_LS = "octopilot.muteAttentionSound";

export function readAttentionSoundMuted() {
  try {
    return localStorage.getItem(ATTENTION_MUTE_LS) === "1";
  } catch {
    return false;
  }
}

export function writeAttentionSoundMuted(muted) {
  try {
    localStorage.setItem(ATTENTION_MUTE_LS, muted ? "1" : "0");
  } catch {
    /* ignore */
  }
  try {
    window.dispatchEvent(new CustomEvent("octopilot-attention-sound-mute-changed"));
  } catch {
    /* ignore */
  }
}

function repoPathToContainerPath(repoRel) {
  const s = String(repoRel || "").replace(/^\//, "");
  if (!s) return "";
  if (s.startsWith("attachments/")) return `/${s}`;
  return `/attachments/${s.replace(/^attachments\/?/i, "")}`;
}

function generatePassword(len = 20) {
  const alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%&*-_=+";
  const arr = new Uint32Array(len);
  crypto.getRandomValues(arr);
  let out = "";
  for (let i = 0; i < len; i++) out += alphabet[arr[i] % alphabet.length];
  return out;
}

function softValidate(item, val, force) {
  if (force) return null;
  const v = item?.validation;
  if (!v) return null;
  const req = Boolean(v.required);
  const empty =
    val == null ||
    (typeof val === "string" && !String(val).trim()) ||
    (Array.isArray(val) && val.length === 0);
  if (req && empty) return "This field is required.";
  if (typeof val === "string" && v.pattern) {
    try {
      const re = new RegExp(v.pattern);
      if (val && !re.test(val)) return "Value does not match the expected format.";
    } catch {
      /* ignore invalid regex from model */
    }
  }
  if (item?.value_kind === "number" && val !== "" && val != null) {
    const n = Number(val);
    if (!Number.isFinite(n)) return "Enter a valid number.";
    if (v.min != null && n < Number(v.min)) return `Must be ≥ ${v.min}.`;
    if (v.max != null && n > Number(v.max)) return `Must be ≤ ${v.max}.`;
  }
  return null;
}

export function HumanInputPanel({ machineId, profileId, pollActive }) {
  const [cur, setCur] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [localVal, setLocalVal] = useState("");
  const [multiSel, setMultiSel] = useState([]);
  const [singleSel, setSingleSel] = useState("");
  const [boolVal, setBoolVal] = useState(null);
  const [promote, setPromote] = useState(false);
  const [clientErr, setClientErr] = useState(null);
  const [showPassword, setShowPassword] = useState(false);
  const [attachmentOptions, setAttachmentOptions] = useState([]);
  const [attachmentsErr, setAttachmentsErr] = useState(null);
  const [attachmentsLoading, setAttachmentsLoading] = useState(false);

  const poll = useCallback(async () => {
    try {
      const res = await fetch(`/api/machines/${machineId}/human-input/current`);
      const j = await res.json().catch(() => ({}));
      if (!res.ok) {
        setErr(j.error || res.statusText || "poll failed");
        return;
      }
      setErr(null);
      setCur(j);
    } catch (e) {
      setErr(e?.message || "poll failed");
    }
  }, [machineId]);

  useEffect(() => {
    if (!pollActive) return undefined;
    poll();
    const t = setInterval(poll, 1500);
    return () => clearInterval(t);
  }, [pollActive, poll]);

  // Re-seed only when the agent opens a *new* prompt (`request_id` changes).
  // Do not depend on `cur.item` — each poll returns a fresh object reference and would
  // reset local state on every tick, wiping in-progress typing (e.g. birthday).
  useEffect(() => {
    if (!cur?.pending || !cur?.item) {
      setLocalVal("");
      setMultiSel([]);
      setSingleSel("");
      setBoolVal(null);
      setPromote(false);
      setClientErr(null);
      setShowPassword(false);
      return;
    }
    const item = cur.item;
    const d = item.default_value;
    const vk = item.value_kind;
    if (vk === "multi_select") {
      if (Array.isArray(d)) {
        setMultiSel(d.map(String));
      } else if (typeof d === "string" && d.trim().startsWith("[")) {
        try {
          const p = JSON.parse(d);
          setMultiSel(Array.isArray(p) ? p.map(String) : []);
        } catch {
          setMultiSel([]);
        }
      } else {
        setMultiSel([]);
      }
    } else if (vk === "single_select") {
      setSingleSel(d != null ? String(d) : "");
    } else if (vk === "boolean") {
      setBoolVal(typeof d === "boolean" ? d : null);
    } else {
      setLocalVal(d != null ? String(d) : "");
    }
    setPromote(false);
    setClientErr(null);
    setShowPassword(false);
  }, [cur?.request_id, cur?.pending]);

  useEffect(() => {
    if (!profileId || !pollActive || !cur?.pending || cur?.item?.value_kind !== "file_path") {
      setAttachmentOptions([]);
      setAttachmentsErr(null);
      setAttachmentsLoading(false);
      return undefined;
    }
    let cancelled = false;
    setAttachmentsLoading(true);
    (async () => {
      try {
        const res = await fetch(`/api/profiles/${encodeURIComponent(profileId)}/attachments`);
        const j = await res.json().catch(() => ({}));
        if (cancelled) return;
        if (!res.ok) {
          setAttachmentsErr(j.error || res.statusText || "failed to load files");
          setAttachmentOptions([]);
          return;
        }
        setAttachmentsErr(null);
        const rows = Array.isArray(j.attachments) ? j.attachments : [];
        setAttachmentOptions(
          rows
            .filter((r) => r && r.exists !== false && r.path)
            .map((r) => ({
              label: `${r.name || r.filename || "file"} (${r.filename || ""})`,
              path: repoPathToContainerPath(r.path),
            }))
        );
      } catch (e) {
        if (!cancelled) {
          setAttachmentsErr(e?.message || "failed to load files");
          setAttachmentOptions([]);
        }
      } finally {
        if (!cancelled) setAttachmentsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [profileId, pollActive, cur?.request_id, cur?.item?.value_kind]);

  const postAnswer = async (body) => {
    if (!cur?.pending || !cur?.request_id) return;
    setBusy(true);
    try {
      const res = await fetch(
        `/api/machines/${machineId}/human-input/requests/${encodeURIComponent(cur.request_id)}/answer`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      );
      const j = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(j.error || res.statusText || "submit failed");
      await poll();
    } catch (e) {
      setClientErr(e?.message || "submit failed");
    } finally {
      setBusy(false);
    }
  };

  const submitField = async (forceSubmit) => {
    const item = cur?.item || {};
    let val = localVal;
    if (item.value_kind === "multi_select") val = multiSel;
    else if (item.value_kind === "single_select") val = singleSel;
    else if (item.value_kind === "boolean") val = boolVal;

    const vErr = softValidate(item, val, forceSubmit);
    if (vErr) {
      setClientErr(vErr);
      return;
    }
    setClientErr(null);
    await postAnswer({
      value: val,
      force_submit: Boolean(forceSubmit),
      promote_to_absolute: promote,
    });
  };

  if (!pollActive) {
    return (
      <div className="human-input-panel human-input-panel--idle">
        Open the <strong>Input</strong> tab to answer agent prompts.
      </div>
    );
  }

  if (err) {
    return (
      <div className="human-input-panel human-input-panel--idle">
        <div className="human-input-err">{err}</div>
      </div>
    );
  }

  if (!cur?.pending) {
    return (
      <div className="human-input-panel human-input-panel--idle">
        <p>No agent prompt right now.</p>
        <p className="human-input-meta">When the agent needs a value or confirmation, it appears here.</p>
      </div>
    );
  }

  const hint = typeof cur.poll_hint_interval_s === "number" ? cur.poll_hint_interval_s : 0.25;
  const kind = cur.kind || "field";
  const item = cur.item || {};
  const title =
    kind === "confirm"
      ? item.title || "Confirm"
      : kind === "captcha_continue"
        ? "Browser checkpoint"
        : item.display_name || item.field_key || "Input needed";

  const vk = item.value_kind;
  const isField = kind === "field" || kind === "document";

  return (
    <div className="human-input-panel">
      <div className="human-input-meta">
        Agent sync ≈ every <strong>{hint < 1 ? hint.toFixed(2) : hint.toFixed(1)}</strong>s (up to 5s after a long
        wait).
      </div>
      <div className="human-input-title">{title}</div>
      {item.help_text || item.body ? (
        <div className="human-input-help">{item.help_text || item.body}</div>
      ) : null}

      {kind === "captcha_continue" ? (
        <div className="human-input-actions">
          <button
            type="button"
            className="btn primary btn--small"
            disabled={busy}
            onClick={() =>
              postAnswer({ continue: true, force_submit: true, promote_to_absolute: false })
            }
          >
            Continue
          </button>
        </div>
      ) : null}

      {kind === "confirm" ? (
        <div className="human-input-actions">
          <button
            type="button"
            className="btn primary btn--small"
            disabled={busy}
            onClick={() => postAnswer({ confirmed: true, force_submit: true, promote_to_absolute: false })}
          >
            Confirm
          </button>
          <button
            type="button"
            className="btn ghost btn--small"
            disabled={busy}
            onClick={() => postAnswer({ confirmed: false, force_submit: true, promote_to_absolute: false })}
          >
            Cancel
          </button>
        </div>
      ) : null}

      {isField && vk === "multiline" ? (
        <div className="human-input-field">
          <label htmlFor={`hi-${cur.request_id}`}>{item.display_name || "Value"}</label>
          <textarea
            id={`hi-${cur.request_id}`}
            className="human-input-control"
            value={localVal}
            onChange={(e) => setLocalVal(e.target.value)}
            autoComplete="off"
          />
          {item.sensitive ? (
            <div className="hi-password-actions">
              <button type="button" className="btn ghost btn--small" onClick={() => setLocalVal(generatePassword())}>
                Generate random password
              </button>
            </div>
          ) : null}
        </div>
      ) : null}

      {isField && vk === "file_path" ? (
        <div className="human-input-field">
          <label htmlFor={`hi-fp-${cur.request_id}`}>{item.display_name || "File"}</label>
          {profileId ? (
            attachmentsLoading ? (
              <div className="human-input-meta">Loading profile files…</div>
            ) : attachmentOptions.length > 0 ? (
              <select
                id={`hi-fp-${cur.request_id}`}
                className="human-input-control hi-select"
                value={attachmentOptions.some((o) => o.path === localVal) ? localVal : ""}
                onChange={(e) => setLocalVal(e.target.value)}
              >
                <option value="">— Select a file —</option>
                {attachmentOptions.map((o) => (
                  <option key={o.path} value={o.path}>
                    {o.label}
                  </option>
                ))}
              </select>
            ) : (
              <>
                <div className="human-input-err">
                  {attachmentsErr || "No files in this profile. Add attachments on the Profiles page, or enter a path."}
                </div>
                <input
                  id={`hi-fp-${cur.request_id}`}
                  className="human-input-control"
                  type="text"
                  value={localVal}
                  onChange={(e) => setLocalVal(e.target.value)}
                  placeholder="/attachments/…"
                />
              </>
            )
          ) : (
            <input
              id={`hi-fp-${cur.request_id}`}
              className="human-input-control"
              type="text"
              value={localVal}
              onChange={(e) => setLocalVal(e.target.value)}
              placeholder="Path inside the machine (e.g. /attachments/…)"
            />
          )}
        </div>
      ) : null}

      {isField && ["text", "number", "date"].includes(vk) ? (
        <div className="human-input-field">
          <label htmlFor={`hi-${cur.request_id}`}>{item.display_name || "Value"}</label>
          <input
            id={`hi-${cur.request_id}`}
            className="human-input-control"
            type={
              item.sensitive && !showPassword ? "password" : vk === "number" ? "number" : vk === "date" ? "date" : "text"
            }
            value={localVal}
            onChange={(e) => setLocalVal(e.target.value)}
            autoComplete={item.sensitive ? "new-password" : "on"}
          />
          {item.sensitive ? (
            <div className="hi-password-actions">
              <button type="button" className="btn ghost btn--small" onClick={() => setShowPassword((v) => !v)}>
                {showPassword ? "Hide password" : "Show password"}
              </button>
              <button type="button" className="btn ghost btn--small" onClick={() => setLocalVal(generatePassword())}>
                Generate random password
              </button>
            </div>
          ) : null}
        </div>
      ) : null}

      {isField && vk === "single_select" && Array.isArray(item.options) ? (
        <div className="human-input-field">
          <div>{item.display_name || "Choose one"}</div>
          <div className="hi-radio-list" role="radiogroup" aria-label={item.display_name}>
            {item.options.map((opt) => {
              const v = String(opt.value ?? "");
              const lab = String(opt.label ?? opt.value ?? "");
              return (
                <label key={v} className="hi-radio-row">
                  <input
                    type="radio"
                    name={`hi-sel-${cur.request_id}`}
                    value={v}
                    checked={singleSel === v}
                    onChange={() => setSingleSel(v)}
                  />
                  <span>{lab}</span>
                </label>
              );
            })}
          </div>
        </div>
      ) : null}

      {isField && vk === "multi_select" && Array.isArray(item.options) ? (
        <div className="human-input-field">
          <div>{item.display_name || "Choose any"}</div>
          <div className="hi-check-list">
            {item.options.map((opt) => {
              const v = String(opt.value ?? "");
              const lab = String(opt.label ?? opt.value ?? "");
              const on = multiSel.includes(v);
              return (
                <label key={v} className="hi-check-row">
                  <input
                    type="checkbox"
                    checked={on}
                    onChange={() => {
                      setMultiSel((prev) => (on ? prev.filter((x) => x !== v) : [...prev, v]));
                    }}
                  />
                  <span>{lab}</span>
                </label>
              );
            })}
          </div>
        </div>
      ) : null}

      {isField && vk === "boolean" ? (
        <div className="human-input-field">
          <div>{item.display_name || "Yes or no"}</div>
          <div className="hi-bool-row">
            <button
              type="button"
              className={`hi-bool-btn ${boolVal === true ? "hi-bool-btn--on" : ""}`}
              onClick={() => setBoolVal(true)}
            >
              Yes
            </button>
            <button
              type="button"
              className={`hi-bool-btn ${boolVal === false ? "hi-bool-btn--on" : ""}`}
              onClick={() => setBoolVal(false)}
            >
              No
            </button>
          </div>
        </div>
      ) : null}

      {isField && item.show_promote_to_absolute ? (
        <label className="hi-promote">
          <input type="checkbox" checked={promote} onChange={(e) => setPromote(e.target.checked)} />
          Promote to absolute (reuse for future applications)
        </label>
      ) : null}

      {clientErr ? <div className="human-input-err">{clientErr}</div> : null}

      {isField ? (
        <div className="human-input-actions">
          <button type="button" className="btn primary btn--small" disabled={busy} onClick={() => submitField(false)}>
            {busy ? "Submitting…" : "Submit"}
          </button>
          <button type="button" className="btn ghost btn--small" disabled={busy} onClick={() => submitField(true)}>
            Force submit
          </button>
        </div>
      ) : null}
    </div>
  );
}
