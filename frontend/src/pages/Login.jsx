import React, { useEffect, useState } from "react";
import { useNavigate, Navigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { LockKey, ArrowRight, Warning } from "@phosphor-icons/react";

// Pulled from sessionStorage on /login mount. Set by AuthContext's
// `risedual:auth-expired` handler so the operator sees WHY they
// were bounced — distinguishes 401 (real auth) vs 5xx (Cloudflare)
// vs cookie-drop next time a logout happens unexpectedly.
function readSessionLost() {
  try {
    const raw = sessionStorage.getItem("risedual_session_lost");
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    return parsed;
  } catch {
    return null;
  }
}

function clearSessionLost() {
  try { sessionStorage.removeItem("risedual_session_lost"); } catch { /* ignore */ }
}

export default function Login() {
  const { user, login, status } = useAuth();
  const nav = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [sessionLost, setSessionLost] = useState(null);

  // Read the session-lost diagnostic ONCE on mount. We don't clear
  // it until the operator successfully signs in (handled by
  // AuthContext.login) or dismisses it manually.
  useEffect(() => {
    setSessionLost(readSessionLost());
  }, []);

  if (status === "loading") return null;
  if (user) return <Navigate to="/admin/hypothesis" replace />;

  const onSubmit = async (e) => {
    e.preventDefault();
    setError("");
    setSubmitting(true);
    const res = await login(email.trim(), password);
    setSubmitting(false);
    if (!res.ok) setError(res.error);
    else nav("/admin/hypothesis", { replace: true });
  };

  return (
    <div
      className="min-h-screen w-full flex bg-rd-bg text-rd-text app-shell"
      data-testid="login-page"
    >
      {/* Left brand panel */}
      <aside className="hidden md:flex md:w-1/2 lg:w-2/5 border-r border-rd-border flex-col justify-between p-10 bg-rd-bg2 relative overflow-hidden">
        <div className="absolute inset-0 opacity-[0.06] pointer-events-none"
          style={{
            backgroundImage:
              "linear-gradient(#fff 1px, transparent 1px), linear-gradient(90deg, #fff 1px, transparent 1px)",
            backgroundSize: "40px 40px",
          }}
        />
        <div className="relative">
          <div className="label-eyebrow mb-3">RISEDUAL // mission control</div>
          <h1 className="font-display text-4xl lg:text-5xl font-black tracking-tighter leading-[0.95]">
            One shared
            <br />
            nervous system.
            <br />
            <span className="text-rd-warn">Four separate brains.</span>
          </h1>
          <p className="mt-6 text-sm text-rd-muted max-w-md leading-relaxed">
            Operator console for the Alpha, Camaro, Chevelle, and REDEYE runtimes. Shared
            infrastructure — isolated decision authority.
          </p>
        </div>
        <div className="relative grid grid-cols-4 gap-px bg-rd-border">
          {[
            { k: "CAMINO", proj: "RISEDUAL-AI-2", c: "#3B82F6" },
            { k: "BARRACUDA", proj: "RD4_0421", c: "#F59E0B" },
            { k: "HELLCAT", proj: "2.1-APP", c: "#10B981" },
            { k: "GTO", proj: "AUDITOR", c: "#06B6D4" },
          ].map((r) => (
            <div key={r.k} className="bg-rd-bg2 p-4">
              <div className="h-1 w-full mb-3" style={{ background: r.c }} />
              <div className="font-display font-bold text-sm">{r.k}</div>
              <div className="text-[10px] text-rd-dim uppercase tracking-widest mt-1">
                {r.proj}
              </div>
            </div>
          ))}
        </div>
      </aside>

      {/* Form */}
      <main className="flex-1 flex items-center justify-center p-6 md:p-10 relative">
        <form
          onSubmit={onSubmit}
          className="w-full max-w-md border border-rd-border bg-rd-bg2 p-8"
          data-testid="login-form"
        >
          <div className="flex items-center gap-2 mb-8">
            <LockKey size={18} weight="bold" className="text-rd-warn" />
            <span className="label-eyebrow">Authenticate operator</span>
          </div>
          <h2 className="font-display text-2xl font-bold tracking-tight mb-6">
            Sign in
          </h2>

          <label className="label-eyebrow block mb-2">Email</label>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full bg-rd-bg border border-rd-border focus:border-rd-warn outline-none px-3 py-3 text-sm font-mono mb-5 text-rd-text"
            placeholder="admin@risedual.io"
            required
            autoComplete="username"
            data-testid="login-email-input"
          />

          <label className="label-eyebrow block mb-2">Password</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full bg-rd-bg border border-rd-border focus:border-rd-warn outline-none px-3 py-3 text-sm font-mono mb-6 text-rd-text"
            placeholder="••••••••••••"
            required
            autoComplete="current-password"
            data-testid="login-password-input"
          />

          {sessionLost && (
            <div
              className="border border-rd-warn/60 bg-rd-warn/5 text-rd-warn px-3 py-2 mb-5 text-[11px] font-mono leading-relaxed flex items-start gap-2"
              data-testid="login-session-lost-banner"
            >
              <Warning size={12} weight="bold" className="mt-0.5 shrink-0" />
              <div className="flex-1">
                <div>
                  <span className="uppercase tracking-widest text-[9px] opacity-80">Session ended</span>
                  {" · "}
                  <span data-testid="login-session-lost-reason">
                    {sessionLost.reason || "unknown"}
                  </span>
                  {sessionLost.status != null && (
                    <span> (HTTP {String(sessionLost.status)})</span>
                  )}
                </div>
                {sessionLost.path && (
                  <div className="opacity-70 break-all">at {sessionLost.path}</div>
                )}
              </div>
              <button
                type="button"
                onClick={() => { clearSessionLost(); setSessionLost(null); }}
                className="opacity-70 hover:opacity-100 text-[10px] uppercase tracking-widest"
                data-testid="login-session-lost-dismiss"
              >
                dismiss
              </button>
            </div>
          )}

          {error && (
            <div
              className="border border-rd-danger text-rd-danger px-3 py-2 mb-5 text-xs font-mono"
              data-testid="login-error"
            >
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={submitting}
            className="btn-sharp w-full bg-zinc-100 text-zinc-900 hover:bg-white disabled:opacity-60 px-4 py-3 flex items-center justify-center gap-2"
            data-testid="login-submit-button"
          >
            {submitting ? "Authenticating…" : "Enter mission control"}
            {!submitting && <ArrowRight size={14} weight="bold" />}
          </button>
        </form>
      </main>
    </div>
  );
}
