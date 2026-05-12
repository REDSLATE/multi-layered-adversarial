/**
 * Public heartbeat ping page — no auth required.
 *
 * Bookmark the URL with the token in the query string and every visit
 * (re)registers a fresh heartbeat for the brain. Auto-refreshes every
 * 30s so leaving the tab open also keeps the row green. Also exposes
 * a BEAT NOW button for one-off pings, and prints the exact URL you'd
 * give to UptimeRobot or any other uptime monitor.
 *
 * Page lives OUTSIDE the authenticated Layout so it's reachable without
 * logging in — that's the whole point: anyone with the token can ping.
 */
import React, { useCallback, useEffect, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";

const BRAIN_META = {
  alpha:    { label: "ALPHA",    color: "#3B82F6" },
  camaro:   { label: "CAMARO",   color: "#F59E0B" },
  chevelle: { label: "CHEVELLE", color: "#10B981" },
  redeye:   { label: "REDEYE",   color: "#DC2626" },
};

function backendBase() {
  // Vite/CRA both expose REACT_APP_BACKEND_URL.
  const b = process.env.REACT_APP_BACKEND_URL || "";
  return b.replace(/\/+$/, "");
}

function fmtRel(iso) {
  if (!iso) return "—";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

export default function Ping() {
  const { brain } = useParams();
  const [params] = useSearchParams();
  const token = params.get("token") || "";
  const meta = BRAIN_META[brain] || { label: brain?.toUpperCase() || "?", color: "#A1A1AA" };

  const [status, setStatus] = useState("idle");      // idle | beating | ok | err
  const [lastSeen, setLastSeen] = useState(null);
  const [error, setError] = useState("");
  const [count, setCount] = useState(0);

  const ping = useCallback(async () => {
    if (!token) { setStatus("err"); setError("missing ?token in URL"); return; }
    setStatus("beating");
    try {
      const r = await fetch(
        `${backendBase()}/api/heartbeat-ping/${brain}?token=${encodeURIComponent(token)}`,
        { method: "GET" }
      );
      const data = await r.json();
      if (!r.ok) {
        setStatus("err");
        setError(data?.detail || `HTTP ${r.status}`);
        return;
      }
      setLastSeen(data.last_seen);
      setCount((n) => n + 1);
      setStatus("ok");
      setError("");
    } catch (e) {
      setStatus("err");
      setError(String(e));
    }
  }, [brain, token]);

  // Fire one beat on mount.
  useEffect(() => { ping(); }, [ping]);

  // Auto-refresh every 30s while tab is open.
  useEffect(() => {
    const id = setInterval(ping, 30000);
    return () => clearInterval(id);
  }, [ping]);

  const pingUrl = token
    ? `${backendBase()}/api/heartbeat-ping/${brain}?token=${token}`
    : `${backendBase()}/api/heartbeat-ping/${brain}?token=<TOKEN>`;

  const ok = status === "ok";

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "#0a0a0a",
        color: "#e5e7eb",
        fontFamily: "ui-monospace, 'SF Mono', Menlo, monospace",
        padding: "32px 24px",
      }}
      data-testid="ping-page"
    >
      <div style={{ maxWidth: 720, margin: "0 auto" }}>
        {/* Brain header */}
        <div style={{
          borderTop: `2px solid ${meta.color}`,
          paddingTop: 20, marginBottom: 28,
        }}>
          <div style={{
            fontSize: 10, letterSpacing: 4, textTransform: "uppercase",
            color: "#71717a", marginBottom: 6,
          }}>
            RISEDUAL · heartbeat ping
          </div>
          <h1 style={{
            fontSize: 48, fontWeight: 900, letterSpacing: -1,
            color: meta.color, margin: 0, lineHeight: 1,
          }} data-testid="ping-brain-label">
            {meta.label}
          </h1>
        </div>

        {/* Status pill */}
        <div style={{
          padding: "16px 20px", marginBottom: 24,
          border: `1px solid ${ok ? "#22c55e" : status === "err" ? "#dc2626" : "#3f3f46"}`,
          background: ok ? "rgba(34,197,94,0.06)" : status === "err" ? "rgba(220,38,38,0.06)" : "transparent",
        }} data-testid="ping-status-box">
          <div style={{ display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
            <span style={{
              fontSize: 11, letterSpacing: 3, textTransform: "uppercase",
              color: ok ? "#22c55e" : status === "err" ? "#dc2626" : "#fbbf24",
              fontWeight: "bold",
            }}>
              {status === "beating" && "▸ BEATING…"}
              {ok && "✓ HEARTBEAT REGISTERED"}
              {status === "err" && "✗ FAILED"}
              {status === "idle" && "◦ IDLE"}
            </span>
            {lastSeen && (
              <span style={{ fontSize: 11, color: "#a1a1aa" }}>
                last beat {fmtRel(lastSeen)}
              </span>
            )}
            {count > 0 && (
              <span style={{ fontSize: 10, color: "#71717a", marginLeft: "auto" }}>
                {count} beat{count === 1 ? "" : "s"} this session
              </span>
            )}
          </div>
          {error && (
            <div style={{ fontSize: 11, color: "#dc2626", marginTop: 8 }} data-testid="ping-error">
              {error}
            </div>
          )}
          {lastSeen && (
            <div style={{ fontSize: 10, color: "#52525b", marginTop: 4 }}>
              {lastSeen}
            </div>
          )}
        </div>

        {/* Beat now */}
        <button
          onClick={ping}
          disabled={status === "beating" || !token}
          data-testid="ping-beat-now-btn"
          style={{
            padding: "12px 24px",
            background: token ? "#e5e7eb" : "#27272a",
            color: token ? "#0a0a0a" : "#71717a",
            border: "none", fontSize: 11, letterSpacing: 3,
            textTransform: "uppercase", fontWeight: "bold",
            cursor: token && status !== "beating" ? "pointer" : "not-allowed",
            marginBottom: 32,
          }}
        >
          {status === "beating" ? "BEATING…" : "BEAT NOW"}
        </button>

        {/* Bookmark / monitor instructions */}
        <div style={{
          border: "1px solid #27272a", padding: "16px 20px", marginBottom: 16,
        }}>
          <div style={{
            fontSize: 10, letterSpacing: 3, textTransform: "uppercase",
            color: "#71717a", marginBottom: 8,
          }}>
            keep this row green permanently
          </div>
          <ol style={{ fontSize: 12, color: "#d4d4d8", paddingLeft: 18, lineHeight: 1.7, margin: 0 }}>
            <li>Bookmark this page — every reload registers a beat.</li>
            <li>
              Or point any uptime monitor (UptimeRobot, BetterUptime,
              healthchecks.io) at the URL below and set the check interval
              to 1–5 min:
            </li>
          </ol>
          <pre style={{
            fontSize: 11, color: meta.color, background: "#000",
            padding: "10px 12px", marginTop: 12, marginBottom: 0,
            overflowX: "auto", whiteSpace: "pre-wrap", wordBreak: "break-all",
            border: "1px solid #18181b",
          }} data-testid="ping-url-block">{pingUrl}</pre>
          {!token && (
            <div style={{ fontSize: 11, color: "#fbbf24", marginTop: 8 }}>
              Add the brain's ingest token as <code>?token=...</code> to
              activate beats. The token lives in the backend .env as{" "}
              <code>{(brain || "").toUpperCase()}_INGEST_TOKEN</code>.
            </div>
          )}
        </div>

        <div style={{
          fontSize: 10, color: "#52525b", lineHeight: 1.7,
          borderTop: "1px solid #18181b", paddingTop: 16,
        }}>
          Doctrinal note: a public ping proves something outside Mission
          Control is regularly calling — stronger than an in-MC proxy
          beater, weaker than a real sidecar that knows the runtime's
          internal state. Replace with a real sidecar when ready.
        </div>
      </div>
    </div>
  );
}
