/**
 * Hostname routing policy.
 *
 * Once DNS is flipped after deploy:
 *   mc.risedual.ai          → operator dashboard ONLY (/admin/*)
 *   mission.risedual.ai     → legacy alias, 301 to mc.risedual.ai
 *   risedual.ai / www.*     → public site ONLY (/, /signals, /markets, etc)
 *   <preview>.emergentagent → dev mode, both surfaces open
 *   localhost / 127.0.0.1   → dev mode, both surfaces open
 *
 * We enforce on the client because both surfaces ship from the same React
 * bundle. The guard renders a redirect (window.location.replace) the moment
 * a wrong-surface URL is detected — much simpler than splitting builds.
 */

const OPERATOR_HOSTS = new Set([
  "mc.risedual.ai",
  "mission.risedual.ai",   // legacy — will 301
]);

const PUBLIC_HOSTS = new Set([
  "risedual.ai",
  "www.risedual.ai",
]);

const DEV_HOST_PATTERNS = [
  /\.preview\.emergentagent\.com$/i,
  /^localhost$/i,
  /^127\.0\.0\.1$/,
  /^0\.0\.0\.0$/,
];

const CANONICAL_OPERATOR_HOST = "mc.risedual.ai";
const CANONICAL_PUBLIC_HOST = "risedual.ai";

export function getHostnameMode(hostname = window.location.hostname) {
  const h = (hostname || "").toLowerCase();
  if (OPERATOR_HOSTS.has(h)) return "operator";
  if (PUBLIC_HOSTS.has(h)) return "public";
  if (DEV_HOST_PATTERNS.some((re) => re.test(h))) return "dev";
  // Unknown host — treat as public (safer default; no operator console leaks)
  return "public";
}

/**
 * Compute the redirect URL (if any) for the current request.
 * Returns null when the URL is already on the correct surface.
 *
 * Rules:
 *   - mission.risedual.ai → always rewrite to mc.risedual.ai (legacy alias).
 *   - On mc.risedual.ai:
 *       /                → /admin
 *       /admin/*         → keep
 *       any public path  → bounce to risedual.ai + same path
 *   - On risedual.ai / www.risedual.ai:
 *       /admin/*         → bounce to mc.risedual.ai/admin/*
 *       /login           → bounce to mc.risedual.ai/login
 *       everything else  → keep
 *   - On dev hosts: never redirect (both surfaces fully accessible).
 */
export function computeHostRedirect(loc = window.location) {
  const host = (loc.hostname || "").toLowerCase();
  const path = loc.pathname || "/";
  const search = loc.search || "";

  // Legacy alias — collapse mission.* → mc.*
  if (host === "mission.risedual.ai") {
    return `https://${CANONICAL_OPERATOR_HOST}${path}${search}`;
  }

  const mode = getHostnameMode(host);

  if (mode === "dev") return null;

  if (mode === "operator") {
    if (path === "/" || path === "") {
      return `https://${CANONICAL_OPERATOR_HOST}/admin${search}`;
    }
    if (path.startsWith("/admin") || path.startsWith("/login") || path.startsWith("/ping")) {
      return null;  // correct surface
    }
    // Anything else on mc.* is a public-site URL — bounce.
    return `https://${CANONICAL_PUBLIC_HOST}${path}${search}`;
  }

  // mode === "public"
  if (path.startsWith("/admin") || path.startsWith("/ping")) {
    return `https://${CANONICAL_OPERATOR_HOST}${path}${search}`;
  }
  if (path.startsWith("/login")) {
    return `https://${CANONICAL_OPERATOR_HOST}${path}${search}`;
  }
  return null;
}
