import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card } from "@/components/ui-bits";
import { Power, ArrowsClockwise, Warning } from "@phosphor-icons/react";

/**
 * MasterTradingSwitch — the *global* kill switch for autonomous order
 * routing. This sits ABOVE the per-lane toggles (LaneExecutionToggles
 * panel). When this is OFF, the auto-router short-circuits on every
 * tick and no orders fire, regardless of lane state, seat state, or
 * broker connectivity.
 *
 * Doctrine pin (2026-02-19):
 *   • OFF is single-click. Kill switches should be fast.
 *   • ON requires confirmation + a non-empty reason. The reason is
 *     written to `shared_trading_state_audit` so every flip is
 *     traceable in a post-mortem.
 *   • If trading_will_fire is False but trading_enabled_runtime is
 *     True, the env-level guard (AUTO_ROUTER_ENABLED=false) is the
 *     blocker — that needs a redeploy, not a click.
 *
 * Wires to:
 *   GET  /admin/trading/status
 *   POST /admin/trading/toggle {enabled, reason}
 *   GET  /admin/trading/audit?limit=5
 */
export default function MasterTradingSwitch() {
  const [state, setState] = useState(null);
  const [audit, setAudit] = useState([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [reason, setReason] = useState("");

  const load = useCallback(async () => {
    try {
      const [{ data: status }, { data: auditData }] = await Promise.all([
        api.get("/admin/trading/status"),
        api.get("/admin/trading/audit?limit=5"),
      ]);
      setState(status);
      setAudit(auditData?.items || []);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e?.message || "fetch failed");
    }
  }, []);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      if (!alive) return;
      await load();
    };
    tick();
    const t = setInterval(tick, 15_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [load]);

  const flipOff = async () => {
    setBusy(true);
    try {
      await api.post("/admin/trading/toggle", {
        enabled: false,
        reason: "operator pulled master switch via UI",
      });
      await load();
    } catch (e) {
      setErr(e?.response?.data?.detail || e?.message || "toggle failed");
    } finally {
      setBusy(false);
    }
  };

  const flipOn = async () => {
    if (!reason.trim()) {
      setErr("reason required to enable trading");
      return;
    }
    setBusy(true);
    try {
      await api.post("/admin/trading/toggle", {
        enabled: true,
        reason: reason.trim(),
      });
      setReason("");
      setConfirming(false);
      await load();
    } catch (e) {
      setErr(e?.response?.data?.detail || e?.message || "toggle failed");
    } finally {
      setBusy(false);
    }
  };

  if (!state) {
    return (
      <Card testid="master-trading-switch-loading">
        <div className="text-rd-dim font-mono text-xs">Loading master switch…</div>
      </Card>
    );
  }

  const runtimeOn = !!state.trading_enabled_runtime;
  const envOn = !!state.trading_enabled_env;
  const willFire = !!state.trading_will_fire;

  // Three-state coloring: green if everything aligned, amber if runtime
  // is on but env is OFF (env-blocked, needs redeploy), red if runtime
  // OFF (operator pause).
  const tone = willFire
    ? { color: "#10B981", label: "ARMED" }
    : runtimeOn && !envOn
      ? { color: "#F59E0B", label: "ENV BLOCKED" }
      : { color: "#DC2626", label: "PAUSED" };

  return (
    <Card testid="master-trading-switch" accentColor={tone.color}>
      <div className="flex items-start gap-3">
        <Power size={20} weight="bold" style={{ color: tone.color }} className="shrink-0 mt-1" />
        <div className="flex-1 min-w-0">
          <div className="flex items-baseline justify-between gap-3 flex-wrap">
            <div>
              <div className="text-[11px] font-mono uppercase tracking-[0.25em] text-rd-text">
                Master Trading Switch
              </div>
              <div className="text-[10px] font-mono text-rd-dim mt-0.5">
                Global kill switch · checked on every auto-router tick
              </div>
            </div>
            <span
              data-testid="master-switch-state"
              className="px-2.5 py-1 text-[10px] font-mono uppercase tracking-widest border rounded-sm"
              style={{ borderColor: tone.color, color: tone.color }}
            >
              {tone.label}
            </span>
          </div>

          <div className="mt-3 grid grid-cols-3 gap-3 text-[10px] font-mono">
            <div>
              <div className="text-rd-dim uppercase tracking-widest">runtime</div>
              <div style={{ color: runtimeOn ? "#10B981" : "#DC2626" }}>
                {runtimeOn ? "ON" : "OFF"}
              </div>
            </div>
            <div>
              <div className="text-rd-dim uppercase tracking-widest">env</div>
              <div style={{ color: envOn ? "#10B981" : "#DC2626" }}>
                {envOn ? "ON" : "OFF"}
              </div>
            </div>
            <div>
              <div className="text-rd-dim uppercase tracking-widest">will_fire</div>
              <div style={{ color: willFire ? "#10B981" : "#DC2626" }}>
                {willFire ? "YES" : "NO"}
              </div>
            </div>
          </div>

          {state.reason && (
            <div
              className="mt-3 p-2 text-[10px] font-mono text-rd-text bg-rd-bg2 border border-rd-border rounded-sm leading-relaxed"
              data-testid="master-switch-current-reason"
            >
              <span className="text-rd-dim uppercase tracking-widest mr-2">last flip:</span>
              {state.reason}
              {state.updated_by && (
                <span className="text-rd-dim ml-2">· by {state.updated_by}</span>
              )}
              {state.updated_at && (
                <span className="text-rd-dim ml-2">· {state.updated_at.slice(0, 16).replace("T", " ")}</span>
              )}
            </div>
          )}

          {err && (
            <div
              className="mt-3 p-2 text-[11px] font-mono text-red-300 bg-red-950/40 border border-red-700 rounded-sm flex items-start gap-2"
              data-testid="master-switch-error"
            >
              <Warning size={14} className="shrink-0 mt-0.5" />
              <span>{err}</span>
            </div>
          )}

          {!runtimeOn && confirming && (
            <div
              className="mt-3 p-3 border border-amber-500/40 bg-amber-500/5 rounded-sm"
              data-testid="master-switch-confirm-on"
            >
              <div className="text-[10px] font-mono uppercase tracking-widest text-amber-300 mb-2">
                Confirm — Enable Autonomous Trading
              </div>
              <div className="text-[11px] font-mono text-rd-text mb-3 leading-relaxed">
                Reason (required, persisted to audit trail):
              </div>
              <textarea
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="e.g. caps verified at $25 per-order / $50 daily; PATENT_SUSPENSION_ACTIVE=False confirmed on prod"
                data-testid="master-switch-reason-input"
                rows={3}
                maxLength={240}
                className="w-full bg-rd-bg border border-rd-border text-rd-text text-[11px] font-mono p-2 rounded-sm resize-y mb-3"
              />
              <div className="flex items-center gap-2">
                <button
                  onClick={flipOn}
                  disabled={busy || !reason.trim()}
                  data-testid="master-switch-confirm-btn"
                  className="px-4 py-1.5 text-[11px] font-mono uppercase tracking-wider border border-emerald-500/60 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20 transition-colors disabled:opacity-40 rounded-sm"
                >
                  {busy ? "Arming…" : "Arm Trading"}
                </button>
                <button
                  onClick={() => { setConfirming(false); setReason(""); setErr(""); }}
                  disabled={busy}
                  data-testid="master-switch-cancel-btn"
                  className="px-4 py-1.5 text-[11px] font-mono uppercase tracking-wider border border-rd-border text-rd-dim hover:text-rd-text transition-colors rounded-sm"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {!confirming && (
            <div className="mt-3 flex items-center gap-2">
              {runtimeOn ? (
                <button
                  onClick={flipOff}
                  disabled={busy}
                  data-testid="master-switch-off-btn"
                  className="flex items-center gap-2 px-4 py-1.5 text-[11px] font-mono uppercase tracking-wider border border-red-600/60 bg-red-600/10 text-red-300 hover:bg-red-600/20 transition-colors disabled:opacity-40 rounded-sm"
                >
                  <Power size={14} />
                  {busy ? "Pausing…" : "Pause Trading"}
                </button>
              ) : (
                <button
                  onClick={() => { setConfirming(true); setErr(""); }}
                  disabled={busy}
                  data-testid="master-switch-on-btn"
                  className="flex items-center gap-2 px-4 py-1.5 text-[11px] font-mono uppercase tracking-wider border border-emerald-500/60 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/20 transition-colors disabled:opacity-40 rounded-sm"
                >
                  <Power size={14} />
                  Re-arm Trading…
                </button>
              )}
              <button
                onClick={load}
                disabled={busy}
                data-testid="master-switch-reload"
                className="flex items-center gap-2 px-3 py-1.5 text-[11px] font-mono uppercase tracking-wider border border-rd-border text-rd-dim hover:text-rd-text transition-colors rounded-sm"
              >
                <ArrowsClockwise size={14} />
                Reload
              </button>
            </div>
          )}

          {audit.length > 0 && (
            <details className="mt-4" data-testid="master-switch-audit">
              <summary className="text-[9px] font-mono uppercase tracking-widest text-rd-dim cursor-pointer hover:text-rd-text">
                Last {audit.length} flips
              </summary>
              <div className="mt-2 border-l border-rd-border pl-3 space-y-1.5">
                {audit.map((row, i) => (
                  <div key={i} className="text-[10px] font-mono leading-relaxed">
                    <span
                      style={{ color: row.enabled ? "#10B981" : "#DC2626" }}
                      className="mr-2"
                    >
                      {row.enabled ? "ON " : "OFF"}
                    </span>
                    <span className="text-rd-dim">{row.ts?.slice(0, 16).replace("T", " ")}</span>
                    <span className="text-rd-dim mx-2">·</span>
                    <span className="text-rd-text">{row.updated_by}</span>
                    {row.reason && (
                      <>
                        <span className="text-rd-dim mx-2">·</span>
                        <span className="text-rd-text">{row.reason}</span>
                      </>
                    )}
                  </div>
                ))}
              </div>
            </details>
          )}
        </div>
      </div>
    </Card>
  );
}
