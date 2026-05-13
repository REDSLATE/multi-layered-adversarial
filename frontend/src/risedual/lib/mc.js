// Public-API client for risedual.ai → Mission Control.
//
// Every call carries the two doctrinal headers MC's public_trust_required
// dependency expects: X-RiseDual-Token (opaque trust token) and
// X-RiseDual-User-Tier (free | starter | pro | pro_max). Both come from
// env / TierContext respectively — never from anything user-input.
//
// All endpoints return JSON. Errors normalize to { ok:false, status, detail }
// so call sites can render without throwing.

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
const PUBLIC_TOKEN = process.env.REACT_APP_RISEDUAL_TOKEN || "";
const BASE = `${BACKEND_URL}/api/public`;

function _headers(tier) {
  return {
    "X-RiseDual-Token": PUBLIC_TOKEN,
    "X-RiseDual-User-Tier": tier || "free",
    "Content-Type": "application/json",
  };
}

async function _get(path, tier, params) {
  let url = `${BASE}${path}`;
  if (params && Object.keys(params).length) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === "") continue;
      qs.append(k, String(v));
    }
    url += `?${qs.toString()}`;
  }
  try {
    const r = await fetch(url, { headers: _headers(tier) });
    const data = await r.json().catch(() => null);
    if (!r.ok) return { ok: false, status: r.status, detail: data?.detail || `HTTP ${r.status}` };
    return { ok: true, status: r.status, data };
  } catch (e) {
    return { ok: false, status: 0, detail: e.message || "Network error" };
  }
}

async function _post(path, tier, body) {
  try {
    const r = await fetch(`${BASE}${path}`, {
      method: "POST",
      headers: _headers(tier),
      body: JSON.stringify(body || {}),
    });
    const data = await r.json().catch(() => null);
    if (!r.ok) return { ok: false, status: r.status, detail: data?.detail || `HTTP ${r.status}` };
    return { ok: true, status: r.status, data };
  } catch (e) {
    return { ok: false, status: 0, detail: e.message || "Network error" };
  }
}

export const mc = {
  signals: (tier, limit = 20) => _get("/signals", tier, { limit }),
  signal: (tier, id) => _get(`/signals/${id}`, tier),
  digest: (tier) => _get("/digest", tier),
  narrative: (tier) => _get("/digest/narrative", tier),
  scannerPresets: (tier) => _get("/scanner/presets", tier),
  scannerScan: (tier, presetId) => _get("/scanner/scan", tier, { preset_id: presetId }),
  heatmap: (tier) => _get("/heatmap", tier),
  sectors: (tier) => _get("/sectors", tier),
  agentActivity: (tier, { since, limit = 50 } = {}) =>
    _get("/agent-activity/feed", tier, { since, limit }),
  modelsMind: (tier) => _get("/models_mind", tier),
  chat: (tier, message, sessionId) => _post("/chat", tier, { message, session_id: sessionId }),
  chatHistory: (tier, sessionId) => _get(`/chat/history/${sessionId}`, tier),
};

export const TIERS = [
  { id: "free", label: "Free" },
  { id: "starter", label: "Starter" },
  { id: "pro", label: "Pro" },
  { id: "pro_max", label: "Pro Max" },
];

export function fmtAgo(iso) {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "—";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
