import axios from "axios";

// Doctrine pin (2026-05-31): when the frontend is hosted on its own
// production domain (e.g., `mission.risedual.ai`), prefer SAME-ORIGIN
// `/api` calls instead of the env-baked cross-origin URL. The prod
// build was historically wired to `multi-brain-backbone.emergent.host`
// while the frontend lives on `mission.risedual.ai` — that worked on
// desktop browsers (CORS configured) but quietly failed on mobile
// Chrome under third-party-cookie / cross-site-fetch restrictions,
// producing the generic "Something went wrong" error post-200-OK.
// Cloudflare on `mission.risedual.ai` proxies `/api` to the same
// backend, so same-origin calls hit the same data with zero cross-site
// surface. Preview / dev environments still use the env var because
// the dev frontend (localhost / *.preview.emergentagent.com) doesn't
// proxy `/api`.
function resolveBackendUrl() {
  const envUrl = process.env.REACT_APP_BACKEND_URL;
  // Server-side / build-time: just use the env value.
  if (typeof window === "undefined") return envUrl;
  const host = window.location.host;
  // Same-origin override list — domains where Cloudflare/ingress is
  // known to proxy `/api/*` to the backend. Add new prod domains here.
  const SAME_ORIGIN_HOSTS = new Set([
    "mission.risedual.ai",
    "www.risedual.ai",
    "risedual.ai",
  ]);
  if (SAME_ORIGIN_HOSTS.has(host)) {
    return `${window.location.protocol}//${host}`;
  }
  return envUrl;
}

const BACKEND_URL = resolveBackendUrl();
export const API = `${BACKEND_URL}/api`;
// Export the resolved backend root so other modules can build their
// own URLs against the same same-origin-vs-env-var policy.
export { BACKEND_URL };

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
    } catch (e) {
      // Defensive: if the detail-extraction logic itself throws, fall
      // back to the default "HTTP <status>" message — but log so we
      // notice the bug instead of silently swallowing it.
      console.warn("[api] error-message extraction failed:", e?.message);
    }
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

// ─── RUNTIME_META ───
// Doctrine pin (2026-06-XX, rev4): this is BRAND metadata only —
// label / color / project / training-intent description. It does
// NOT define authority. Authority lives on SEATS (see RosterPanel
// + ROLE_META). The `role` / `roleTagline` / `enforceFlag` fields
// are legacy and retained ONLY so any older bundle reading them
// doesn't blank-render. Any code that grants or denies execution
// based on these fields is a bug and must be removed.
//
// Identity convention: internal DB / API keys remain
//   alpha / camaro / chevelle / redeye  (slot codes — never user-facing)
// Display labels are the operator brand:
//   Camino / Barracuda / Hellcat / GTO  (rendered everywhere in the UI)
export const RUNTIME_META = {
  alpha: {
    label: "CAMINO",
    project: "RISEDUAL-AI-2",
    color: "#3B82F6",
    accentClass: "text-rd-alpha",
    borderClass: "border-rd-alpha",
    bgClass: "bg-rd-alpha",
    enforceFlag: null,
    enforceLabel: null,
    role: null,
    roleTitle: "Camino",
    roleTagline: "structured trader",
    note: "Trader-trained · structured-signal-first.",
  },
  camaro: {
    label: "BARRACUDA",
    project: "RD4_0421",
    color: "#F59E0B",
    accentClass: "text-rd-camaro",
    borderClass: "border-rd-camaro",
    bgClass: "bg-rd-camaro",
    enforceFlag: null,
    enforceLabel: null,
    role: null,
    roleTitle: "Barracuda",
    roleTagline: "challenger / counterfactual",
    note: "Challenger-trained · attacks the thesis, surfaces counterfactuals.",
  },
  chevelle: {
    label: "HELLCAT",
    project: "2.1-APP",
    color: "#10B981",
    accentClass: "text-rd-chevelle",
    borderClass: "border-rd-chevelle",
    bgClass: "bg-rd-chevelle",
    enforceFlag: null,
    enforceLabel: null,
    role: null,
    roleTitle: "Hellcat",
    roleTagline: "memory + calibration",
    note: "Governor-trained · memory firewall, readiness, calibration, audit.",
  },
  redeye: {
    label: "GTO",
    project: "REDEYE",
    color: "#DC2626",
    accentClass: "text-rd-redeye",
    borderClass: "border-rd-redeye",
    bgClass: "bg-rd-redeye",
    enforceFlag: null,
    enforceLabel: null,
    role: null,
    roleTitle: "GTO",
    roleTagline: "adversarial scout",
    note: "Adversarial-trained · stamps the contrary case on every position.",
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
