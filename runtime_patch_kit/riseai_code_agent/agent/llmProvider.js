/**
 * LLM provider abstraction — direct HTTPS calls to Anthropic, OpenAI,
 * and Gemini.
 *
 * Doctrine:
 *   This module is the portability seam. The CLI never knows which
 *   provider it's talking to — it asks `callLLM({provider, ...})`
 *   and gets a normalized `{text, usage, raw}` back. When the
 *   operator leaves the Emergent platform, the only thing that
 *   changes is which API key env var is populated; the call site
 *   doesn't change.
 *
 * Key env vars (one of these must be set for the chosen provider):
 *   anthropic  → ANTHROPIC_API_KEY     (starts with `sk-ant-`)
 *   openai     → OPENAI_API_KEY        (starts with `sk-`)
 *   gemini     → GEMINI_API_KEY        (starts with `AIza`)
 *
 * Zero external dependencies — uses Node 18+ native `fetch`. The
 * CLI's package.json doesn't gain any LLM SDK package, so the kit
 * stays portable and easy to audit.
 */


export const PROVIDERS = ["anthropic", "openai", "gemini"];


const DEFAULT_MODELS = {
  anthropic: "claude-sonnet-4-5-20250929",
  openai: "gpt-5.1",
  gemini: "gemini-2.5-pro",
};


const ENV_KEYS = {
  anthropic: "ANTHROPIC_API_KEY",
  openai: "OPENAI_API_KEY",
  gemini: "GEMINI_API_KEY",
};


/**
 * Resolve the default model for a provider when the operator hasn't
 * passed `--model`. Public so the CLI can echo what's about to be
 * called before making the network round-trip.
 */
export function defaultModel(provider) {
  if (!DEFAULT_MODELS[provider]) {
    throw new Error(`unknown provider: ${provider}`);
  }
  return DEFAULT_MODELS[provider];
}


/**
 * Resolve the API key from environment. Returns `null` if missing so
 * the CLI can emit a friendly "set $X" message instead of leaking
 * a generic 401 from the provider.
 */
export function resolveApiKey(provider) {
  const envName = ENV_KEYS[provider];
  if (!envName) {
    throw new Error(`unknown provider: ${provider}`);
  }
  const value = process.env[envName] || "";
  return { envName, value: value.trim() || null };
}


/**
 * Main entry point.
 *
 * @param {object} params
 * @param {"anthropic"|"openai"|"gemini"} params.provider
 * @param {string} [params.model]
 * @param {string} params.system    System prompt (role/instructions)
 * @param {string} params.user      User prompt (question + context)
 * @param {string} [params.apiKey]  Override env-resolved key
 * @param {number} [params.maxTokens]
 * @param {number} [params.timeoutMs]
 * @returns {Promise<{text: string, model: string, usage: object|null, raw: object}>}
 */
export async function callLLM({
  provider,
  model,
  system,
  user,
  apiKey,
  maxTokens = 4096,
  timeoutMs = 90_000,
}) {
  if (!PROVIDERS.includes(provider)) {
    throw new Error(
      `unsupported provider '${provider}'; expected one of ${PROVIDERS.join(", ")}`,
    );
  }
  const resolvedModel = model || defaultModel(provider);
  const key = apiKey || resolveApiKey(provider).value;
  if (!key) {
    const envName = ENV_KEYS[provider];
    throw new Error(
      `no API key for ${provider}; set ${envName} in your environment`,
    );
  }

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    if (provider === "anthropic") {
      return await callAnthropic({ apiKey: key, model: resolvedModel, system, user, maxTokens, signal: ctrl.signal });
    }
    if (provider === "openai") {
      return await callOpenAI({ apiKey: key, model: resolvedModel, system, user, maxTokens, signal: ctrl.signal });
    }
    if (provider === "gemini") {
      return await callGemini({ apiKey: key, model: resolvedModel, system, user, maxTokens, signal: ctrl.signal });
    }
    throw new Error(`provider router missed: ${provider}`);
  } finally {
    clearTimeout(timer);
  }
}


// ──────────────────────────── Anthropic ───────────────────────────────


async function callAnthropic({ apiKey, model, system, user, maxTokens, signal }) {
  const url = "https://api.anthropic.com/v1/messages";
  const body = {
    model,
    max_tokens: maxTokens,
    system,
    messages: [{ role: "user", content: user }],
  };
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": apiKey,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify(body),
    signal,
  });
  const raw = await safeJson(resp);
  if (!resp.ok) {
    const detail = raw?.error?.message || resp.statusText || `HTTP ${resp.status}`;
    throw new Error(`anthropic HTTP ${resp.status}: ${detail}`);
  }
  // Response shape: { content: [{type:"text", text:"..."}], usage: {input_tokens, output_tokens} }
  const text = (raw?.content || [])
    .filter((c) => c.type === "text")
    .map((c) => c.text)
    .join("");
  return { text, model, usage: raw?.usage || null, raw };
}


// ──────────────────────────── OpenAI ──────────────────────────────────


async function callOpenAI({ apiKey, model, system, user, maxTokens, signal }) {
  const url = "https://api.openai.com/v1/chat/completions";
  const body = {
    model,
    max_completion_tokens: maxTokens,
    messages: [
      { role: "system", content: system },
      { role: "user", content: user },
    ],
  };
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify(body),
    signal,
  });
  const raw = await safeJson(resp);
  if (!resp.ok) {
    const detail = raw?.error?.message || resp.statusText || `HTTP ${resp.status}`;
    throw new Error(`openai HTTP ${resp.status}: ${detail}`);
  }
  const text = raw?.choices?.[0]?.message?.content || "";
  return { text, model, usage: raw?.usage || null, raw };
}


// ──────────────────────────── Gemini ──────────────────────────────────


async function callGemini({ apiKey, model, system, user, maxTokens, signal }) {
  // Google's text-generation API. The system instruction is folded
  // into `systemInstruction` to keep the user/system separation we
  // already use for anthropic/openai.
  const url = `https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(model)}:generateContent?key=${encodeURIComponent(apiKey)}`;
  const body = {
    systemInstruction: { role: "system", parts: [{ text: system }] },
    contents: [{ role: "user", parts: [{ text: user }] }],
    generationConfig: { maxOutputTokens: maxTokens },
  };
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  const raw = await safeJson(resp);
  if (!resp.ok) {
    const detail = raw?.error?.message || resp.statusText || `HTTP ${resp.status}`;
    throw new Error(`gemini HTTP ${resp.status}: ${detail}`);
  }
  const parts = raw?.candidates?.[0]?.content?.parts || [];
  const text = parts.map((p) => p.text || "").join("");
  return { text, model, usage: raw?.usageMetadata || null, raw };
}


// ──────────────────────────── helpers ─────────────────────────────────


async function safeJson(resp) {
  try {
    return await resp.json();
  } catch (e) {
    return null;
  }
}
