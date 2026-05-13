import axios from "axios";

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;
export const API = `${BACKEND_URL}/api`;

const TOKEN_KEY = "risedual_access_token";

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch (e) {
    // localStorage can be denied in some sandboxed iframes / Safari
    // private mode; warn so we know why auth feels broken in those
    // environments, but never throw.
    console.warn("[api] getToken: localStorage unavailable —", e?.message);
    return null;
  }
}
export function setToken(t) {
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  } catch (e) {
    console.warn("[api] setToken: localStorage unavailable —", e?.message);
  }
}

// ──────────────────────────────────────────────────────────────────────
// fetch-based client. Replaces axios because axios 1.x's XHR adapter
// intermittently hangs (request sent, response never returns to the JS
// promise) under our Cloudflare-fronted preview deploy. Surface matches
// what callers already use: api.get/post/put/delete returning {data},
// errors with shape err.response.{status,data}.
// ──────────────────────────────────────────────────────────────────────

function buildUrl(path, params) {
  let url = path.startsWith("http") ? path : `${API}${path.startsWith("/") ? path : `/${path}`}`;
  if (params && Object.keys(params).length) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null) continue;
      qs.append(k, String(v));
    }
    const sep = url.includes("?") ? "&" : "?";
    url += sep + qs.toString();
  }
  return url;
}

async function request(method, path, body, cfg = {}) {
  const url = buildUrl(path, cfg.params);
  const headers = { ...(cfg.headers || {}) };
  const tok = getToken();
  if (tok && !headers.Authorization) headers.Authorization = `Bearer ${tok}`;
  if (body !== undefined && !headers["Content-Type"]) headers["Content-Type"] = "application/json";

  let resp;
  try {
    resp = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    const err = new Error(e.message || "Network error");
    err.response = null;
    throw err;
  }

  const ct = resp.headers.get("content-type") || "";
  let data = null;
  // Snapshot the body as text first — body can only be consumed once,
  // and we need a fallback path when JSON parsing fails (proxy strips
  // content-type, server returns half-formed JSON, etc.).
  let rawText = "";
  try { rawText = await resp.text(); } catch { rawText = ""; }
  if (rawText) {
    if (ct.includes("application/json") || rawText.trim().startsWith("{") || rawText.trim().startsWith("[")) {
      try { data = JSON.parse(rawText); } catch { data = rawText; }
    } else {
      data = rawText;
    }
  }

  if (!resp.ok) {
    // Surface a helpful message by default. Components can still read
    // err.response.{status,data} for structured handling.
    let msg = `HTTP ${resp.status}`;
    try {
      if (data && typeof data === "object") {
        const detail = data.detail;
        if (typeof detail === "string" && detail.trim()) {
          msg = detail;
        } else if (Array.isArray(detail) && detail.length) {
          // FastAPI validation errors: array of {msg, loc, type}
          msg = detail
            .map((d) => (d && typeof d.msg === "string" ? d.msg : ""))
            .filter(Boolean)
            .join(" · ") || msg;
        } else if (detail && typeof detail === "object" && typeof detail.reason === "string") {
          msg = detail.reason;
        }
      } else if (typeof data === "string" && data.trim()) {
        msg = data.length > 400 ? `${data.slice(0, 400)}…` : data;
      }
    } catch (_e) { /* keep default msg */ }
    const err = new Error(msg);
    err.response = { status: resp.status, data, rawText };
    throw err;
  }
  return { data, status: resp.status };
}

export const api = {
  get:    (path, cfg) => request("GET", path, undefined, cfg),
  post:   (path, body, cfg) => request("POST", path, body ?? {}, cfg),
  put:    (path, body, cfg) => request("PUT", path, body ?? {}, cfg),
  patch:  (path, body, cfg) => request("PATCH", path, body ?? {}, cfg),
  delete: (path, cfg) => request("DELETE", path, undefined, cfg),
};

// Keep axios import alive so existing usages of `axios` directly (if any
// future code reaches for it) still resolve. The exported `api` above is
// the only client used by the app today.
export const _axios = axios;

export function formatApiErrorDetail(detail) {
  if (detail == null) return "Something went wrong. Please try again.";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail))
    return detail
      .map((e) => (e && typeof e.msg === "string" ? e.msg : JSON.stringify(e)))
      .filter(Boolean)
      .join(" ");
  if (detail && typeof detail.msg === "string") return detail.msg;
  return String(detail);
}

export const RUNTIME_META = {
  alpha: {
    label: "ALPHA",
    project: "RISEDUAL-AI-2",
    color: "#3B82F6",
    accentClass: "text-rd-alpha",
    borderClass: "border-rd-alpha",
    bgClass: "bg-rd-alpha",
    enforceFlag: "alpha_phase6_enforce_enabled",
    enforceLabel: "PHASE6_ENFORCE_ENABLED",
    role: "trader",
    roleTitle: "Trader",
    roleTagline: "has hands",
    note: "Generates executable signals — only stack eligible for live execution",
  },
  camaro: {
    label: "CAMARO",
    project: "RD4_0421",
    color: "#F59E0B",
    accentClass: "text-rd-camaro",
    borderClass: "border-rd-camaro",
    bgClass: "bg-rd-camaro",
    enforceFlag: "camaro_executor_enforce_enabled",
    enforceLabel: "CAMARO_EXECUTOR_ENFORCE_ENABLED",
    role: "challenger",
    roleTitle: "Challenger",
    roleTagline: "has teeth",
    note: "Shadows Alpha · attacks the thesis · cannot place trades",
  },
  chevelle: {
    label: "CHEVELLE",
    project: "2.1-APP",
    color: "#10B981",
    accentClass: "text-rd-chevelle",
    borderClass: "border-rd-chevelle",
    bgClass: "bg-rd-chevelle",
    enforceFlag: "chevelle_authority_enabled",
    enforceLabel: "CHEVELLE_AUTHORITY_ENABLED",
    role: "governor",
    roleTitle: "Governor",
    roleTagline: "has the keys",
    note: "Memory firewall · readiness · calibration · audit · promotion",
  },
  redeye: {
    label: "REDEYE",
    project: "REDEYE",
    color: "#DC2626",
    accentClass: "text-rd-redeye",
    borderClass: "border-rd-redeye",
    bgClass: "bg-rd-redeye",
    enforceFlag: null,            // no per-runtime enforce flag yet
    enforceLabel: null,
    role: "opponent",
    roleTitle: "Opponent",
    roleTagline: "argues the contrary case",
    note: "Adversarial scout · stamps the contrary case on every position · cannot execute",
  },
};

export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toISOString().replace("T", " ").replace("Z", "Z").slice(0, 19) + "Z";
}

export function relTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}
