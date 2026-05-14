/**
 * Hostname routing policy.
 *
 *   mission.risedual.ai     → operator dashboard ONLY (/admin/*)
 *   risedual.ai / www.*     → public site ONLY (/, /signals, /markets, etc)
 *   <preview>.emergentagent → dev mode, both surfaces open
 *   localhost / 127.0.0.1   → dev mode, both surfaces open
 *
 * Operator surface is canonically `mission.risedual.ai`.
 *
 * We enforce on the client because both surfaces ship from the same React
 * bundle. The guard renders a redirect (window.location.replace) the moment
 * a wrong-surface URL is detected — much simpler than splitting builds.
 */

const CANONICAL_OPERATOR_HOST = "mission.risedual.ai";
const CANONICAL_PUBLIC_HOST = "risedual.ai";

const OPERATOR_HOSTS = new Set([
  CANONICAL_OPERATOR_HOST,
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
 *   - On mission.risedual.ai (operator):
 *       /                → /admin
 *       /admin/*         → keep
 *       any public path  → bounce to risedual.ai + same path
 *   - On risedual.ai / www.risedual.ai (public):
 *       /admin/*         → bounce to mission.risedual.ai/admin/*
 *       /login           → bounce to mission.risedual.ai/login
 *       everything else  → keep
 *   - On dev hosts: never redirect (both surfaces fully accessible).
 */
export function computeHostRedirect(loc = window.location) {
  const host = (loc.hostname || "").toLowerCase();
  const path = loc.pathname || "/";
  const search = loc.search || "";

  const mode = getHostnameMode(host);

  if (mode === "dev") return null;

  if (mode === "operator") {
    if (path === "/" || path === "") {
      return `https://${CANONICAL_OPERATOR_HOST}/admin${search}`;
    }
    if (path.startsWith("/admin") || path.startsWith("/login") || path.startsWith("/ping")) {
      return null;  // correct surface
    }
    // Anything else on the operator host is a public-site URL — bounce.
    return `https://${CANONICAL_PUBLIC_HOST}${path}${search}`;
  }

  // mode === "public"
  if (path.startsWith("/admin") || path.startsWith("/ping") || path.startsWith("/login")) {
    return `https://${CANONICAL_OPERATOR_HOST}${path}${search}`;
  }
  return null;
}
