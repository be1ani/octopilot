/**
 * Catalog of LLM model IDs the agent can run with.
 *
 * Curated from each provider's docs:
 *   OpenAI    https://platform.openai.com/docs/models
 *   DeepSeek  https://api-docs.deepseek.com/quick_start/pricing
 *
 * The orchestrator backend matches the chosen model id against
 * `LLM_PROVIDER_REGISTRY.model_prefixes` to pick the right provider, so any
 * id that starts with `gpt-`, `o1`/`o3`/`o4`, `chatgpt-`, `computer-use-`,
 * `text-embedding-` or `deepseek-` is dispatched correctly. New families can
 * be added here without touching the dispatch code.
 */
export const DEFAULT_LLM_MODEL = "gpt-5.4";

/** @type {readonly string[]} */
export const LLM_MODEL_IDS = Object.freeze(
  [
    "chatgpt-4o-latest",
    "computer-use-preview",
    "gpt-3.5-turbo",
    "gpt-3.5-turbo-0125",
    "gpt-3.5-turbo-1106",
    "gpt-3.5-turbo-16k",
    "gpt-4",
    "gpt-4-0125-preview",
    "gpt-4-0613",
    "gpt-4-1106-preview",
    "gpt-4-32k",
    "gpt-4-turbo",
    "gpt-4-turbo-preview",
    "gpt-4.1",
    "gpt-4.1-2025-04-14",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-4.5-preview",
    "gpt-4o",
    "gpt-4o-2024-05-13",
    "gpt-4o-2024-08-06",
    "gpt-4o-2024-11-20",
    "gpt-4o-mini",
    "gpt-4o-mini-search-preview",
    "gpt-4o-search-preview",
    "gpt-5",
    "gpt-5-chat-latest",
    "gpt-5-codex",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-5-pro",
    "gpt-5.1",
    "gpt-5.2",
    "gpt-5.2-chat-latest",
    "gpt-5.3",
    "gpt-5.3-chat-latest",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.4-pro",
    "o1",
    "o1-mini",
    "o1-preview",
    "o1-pro",
    "o3",
    "o3-mini",
    "o3-pro",
    "o4-mini",
    // DeepSeek (https://api-docs.deepseek.com/quick_start/pricing).
    // Current API: `deepseek-v4-flash` / `deepseek-v4-pro`. Legacy aliases
    // `deepseek-chat` / `deepseek-reasoner` still work but are deprecated.
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
  ].sort((a, b) => a.localeCompare(b))
);

// Back-compat aliases — older imports (`CHATGPT_MODEL_IDS`,
// `DEFAULT_CHATGPT_MODEL`) keep working while the catalog grows beyond
// OpenAI. Prefer the `LLM_MODEL_IDS` / `DEFAULT_LLM_MODEL` names in new code.
export const CHATGPT_MODEL_IDS = LLM_MODEL_IDS;
export const DEFAULT_CHATGPT_MODEL = DEFAULT_LLM_MODEL;
