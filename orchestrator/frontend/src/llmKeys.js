const STORAGE_KEY = "octopilot.llmKeys";

export function readLlmKeys() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { openai: "", openai_admin: "", anthropic: "", google: "" };
    const obj = JSON.parse(raw);
    return {
      openai: typeof obj?.openai === "string" ? obj.openai : "",
      openai_admin: typeof obj?.openai_admin === "string" ? obj.openai_admin : "",
      anthropic: typeof obj?.anthropic === "string" ? obj.anthropic : "",
      google: typeof obj?.google === "string" ? obj.google : "",
    };
  } catch {
    return { openai: "", openai_admin: "", anthropic: "", google: "" };
  }
}

export function writeLlmKeys(next) {
  const safe = {
    openai: String(next?.openai || "").trim(),
    openai_admin: String(next?.openai_admin || "").trim(),
    anthropic: String(next?.anthropic || "").trim(),
    google: String(next?.google || "").trim(),
  };
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(safe));
  } catch {
    /* ignore */
  }
  return safe;
}

export function clearLlmKeys() {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

export function llmKeyHeaders(keys = readLlmKeys()) {
  const h = {};
  if (keys?.openai) h["X-OpenAI-Api-Key"] = keys.openai;
  if (keys?.openai_admin) h["X-OpenAI-Admin-Key"] = keys.openai_admin;
  if (keys?.anthropic) h["X-Anthropic-Api-Key"] = keys.anthropic;
  if (keys?.google) h["X-Google-Api-Key"] = keys.google;
  return h;
}

