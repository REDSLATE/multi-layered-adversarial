import React, { useState } from "react";
import { useNavigate, Navigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import { LockKey, ArrowRight } from "@phosphor-icons/react";

export default function Login() {
  const { user, login, status } = useAuth();
  const nav = useNavigate();
  const [email, setEmail] = useState("admin@risedual.io");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  if (status === "loading") return null;
  if (user) return <Navigate to="/" replace />;

  const onSubmit = async (e) => {
    e.preventDefault();
    setError("");
    setSubmitting(true);
    const res = await login(email.trim(), password);
    setSubmitting(false);
    if (!res.ok) setError(res.error);
    else nav("/", { replace: true });
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
            <span className="text-rd-warn">Three separate brains.</span>
          </h1>
          <p className="mt-6 text-sm text-rd-muted max-w-md leading-relaxed">
            Operator console for the Alpha, Camaro, and Chevelle runtimes. Shared
            infrastructure — isolated decision authority.
          </p>
        </div>
        <div className="relative grid grid-cols-3 gap-px bg-rd-border">
          {[
            { k: "ALPHA", proj: "RISEDUAL-AI-2", c: "#3B82F6" },
            { k: "CAMARO", proj: "RD4_0421", c: "#F59E0B" },
            { k: "CHEVELLE", proj: "2.1-APP", c: "#10B981" },
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

          <div className="mt-8 pt-6 border-t border-rd-border text-[10px] uppercase tracking-widest text-rd-dim leading-relaxed">
            Observation-only deploy ·{" "}
            <span className="text-rd-warn">execution disabled</span>
          </div>
        </form>
      </main>
    </div>
  );
}
