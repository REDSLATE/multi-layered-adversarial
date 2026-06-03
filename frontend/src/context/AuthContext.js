import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api, formatApiErrorDetail, getToken, setToken } from "@/lib/api";

const AuthContext = createContext(null);

// Doctrine pin (2026-02-17, rev3): token-clearing must be CONSERVATIVE.
// A single transient 5xx / network blip from MC's `/auth/me` endpoint
// MUST NOT log the operator out — they are mid-incident-response when
// it happens. Only 401/403 (the server has genuinely rejected the
// token) is a real auth failure. Anything else is retried with backoff.
const AUTH_ERROR_STATUSES = new Set([401, 403]);
const RETRY_DELAYS_MS = [500, 1500, 3000]; // 3 retries → ~5s of patience

function isAuthRejection(err) {
  // err.response === null  → fetch threw (offline / DNS / CORS preflight
  // fail / Cloudflare drop). NOT an auth rejection; retry.
  // err.response.status in {401,403} → server says token is bad. Clear it.
  // Anything else (404 / 5xx) is a transient backend problem, not the
  // operator's session being invalid.
  const status = err?.response?.status;
  return typeof status === "number" && AUTH_ERROR_STATUSES.has(status);
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [status, setStatus] = useState("loading");

  useEffect(() => {
    let mounted = true;
    (async () => {
      const t = getToken();
      if (!t) {
        if (mounted) {
          setUser(null);
          setStatus("ready");
        }
        return;
      }

      // Attempt /auth/me with retry-on-transient-error. We tolerate
      // 5xx / network failures (MC redeploying, brief Cloudflare blip)
      // by retrying; only an explicit 401/403 from the server kills
      // the local token.
      const attempts = 1 + RETRY_DELAYS_MS.length;
      let lastErr = null;
      for (let i = 0; i < attempts; i += 1) {
        try {
          const { data } = await api.get("/auth/me");
          if (!mounted) return;
          setUser(data);
          setStatus("ready");
          return;
        } catch (e) {
          lastErr = e;
          if (isAuthRejection(e)) {
            // Real rejection — token is invalid. Purge it.
            setToken(null);
            if (mounted) {
              setUser(null);
              setStatus("ready");
            }
            return;
          }
          // Transient: keep the token, wait, retry.
          if (i < attempts - 1) {
            const waitMs = RETRY_DELAYS_MS[i];
            // eslint-disable-next-line no-console
            console.warn(
              `[auth] /auth/me transient failure (attempt ${i + 1}/${attempts}); retrying in ${waitMs}ms —`,
              e?.response?.status ?? e?.message,
            );
            await delay(waitMs);
            if (!mounted) return;
          }
        }
      }

      // All retries exhausted on transient errors. KEEP the token in
      // localStorage so the next page load / manual refresh can re-auth
      // once MC is healthy. We mark status ready with user=null so the
      // app falls through to /login rather than hanging on "Authenticating"
      // forever, but the operator can sign in again without losing their
      // saved token state.
      // eslint-disable-next-line no-console
      console.error(
        "[auth] /auth/me exhausted retries; keeping token, falling through to login —",
        lastErr?.response?.status ?? lastErr?.message,
      );
      if (mounted) {
        setUser(null);
        setStatus("ready");
      }
    })();
    return () => {
      mounted = false;
    };
  }, []);

  const login = useCallback(async (email, password) => {
    try {
      const { data } = await api.post("/auth/login", { email, password });
      setToken(data.access_token);
      setUser(data.user);
      return { ok: true };
    } catch (e) {
      // Distinguish three failure modes so the operator sees the
      // ACTUAL cause, not a generic "Something went wrong":
      //   1. Server responded with a body (e.g. 401 + detail) → show detail
      //   2. Request reached fetch but failed (network / CORS) → show e.message
      //   3. Truly unknown → final fallback
      const detail = e?.response?.data?.detail;
      let msg;
      if (detail != null) {
        msg = formatApiErrorDetail(detail);
      } else if (typeof e?.message === "string" && e.message.trim()) {
        // Network-class error. fetch threw, no response body.
        msg = `Cannot reach Mission Control: ${e.message}`;
      } else {
        msg = "Login failed. Please try again.";
      }
      return { ok: false, error: msg };
    }
  }, []);

  const logout = useCallback(async () => {
    setToken(null);
    setUser(null);
  }, []);

  const value = useMemo(
    () => ({ user, status, login, logout }),
    [user, status, login, logout],
  );

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
