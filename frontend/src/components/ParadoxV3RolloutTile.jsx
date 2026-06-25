/**
 * ParadoxV3RolloutTile — one-glance v3 rollout status (operator pin
 * 2026-02-22).
 *
 * Polls three read-only endpoints every 10s:
 *   GET /api/admin/paradox-v3/status                — flags + rollout step
 *   GET /api/admin/paradox-v3/execution-style-outcomes — per-style table
 *   GET /api/admin/doctrine/retirement-candidates    — v3 PATIENT count
 *
 * (Calls drop the `/api` prefix locally — `api.js` prepends it.)
 *
 * Surfaces:
 *   • Brains: ✓/○ per brain (camino, barracuda, hellcat, gto)
 *   • Trigger watcher posture + refire posture
 *   • Patient outcomes progress: N / 50 (READY threshold)
 *   • Per-execution-style table (win rate, avg pnl, state band)
 *   • Retirement candidates flagged for v3 PATIENT scope
 *   • Overall Execution Judge state: LEARNING / READY / TRIPPED
 *
 * READ-ONLY. No env-flag mutation, no rollout actions.
 */
import { useCallback, useEffect, useState } from "react";
import { ArrowsClockwise, CheckCircle, Circle, Warning } from "@phosphor-icons/react";
import { api } from "@/lib/api";

const POLL_MS = 10_000;
const ALL_BRAINS = ["camino", "barracuda", "hellcat", "gto"];
const READY_THRESHOLD = 50;  // pinned by operator — don't lower


function StateBadge({ state }) {
  // Conservative band styling — STRONG/HIGH_CONVICTION green, READY
  // amber, LEARNING grey, INSUFFICIENT dim.
  const styles = {
    HIGH_CONVICTION: { fg: "#10B981", label: "HIGH" },
    STRONG:          { fg: "#10B981", label: "STRONG" },
    READY:           { fg: "#F59E0B", label: "READY" },
    LEARNING:        { fg: "#6B7280", label: "LEARNING" },
    INSUFFICIENT:    { fg: "#374151", label: "—" },
  };
  const cfg = styles[state] || styles.INSUFFICIENT;
  return (
    <span
      className="font-mono text-[9px] uppercase tracking-widest px-1.5 py-0.5 border"
      style={{ color: cfg.fg, borderColor: cfg.fg + "55" }}
      data-testid={`v3-band-${state.toLowerCase()}`}
    >
      {cfg.label}
    </span>
  );
}


function ProgressBar({ value, max }) {
  const pct = Math.min(100, Math.max(0, (value / max) * 100));
  const colour = value >= max ? "#10B981" : "#F59E0B";
  return (
    <div className="relative h-1.5 bg-rd-border w-full overflow-hidden">
      <div
        className="absolute inset-y-0 left-0"
        style={{ width: `${pct}%`, backgroundColor: colour }}
      />
    </div>
  );
}


function classifyJudgeState(stylesData, retirementCount) {
  // PATIENT trade count drives the Execution Judge state surface.
  const patient = (stylesData?.styles || []).find(
    (s) => s.execution_style === "PATIENT",
  );
  const trades = patient?.trades || 0;
  if (retirementCount > 0) return "TRIPPED";
  if (trades >= READY_THRESHOLD) return "READY";
  return "LEARNING";
}


export default function ParadoxV3RolloutTile() {
  const [status, setStatus] = useState(null);
  const [styles, setStyles] = useState(null);
  const [retirement, setRetirement] = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const [lastRefresh, setLastRefresh] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const [s, st, ret] = await Promise.all([
        api.get("/admin/paradox-v3/status"),
        api.get("/admin/paradox-v3/execution-style-outcomes"),
        // Retirement candidates endpoint may not be mounted in all
        // deploys — soft-fail to null rather than break the tile.
        api.get("/admin/doctrine/retirement-candidates")
           .catch(() => ({ candidates: [] })),
      ]);
      setStatus(s);
      setStyles(st);
      setRetirement(ret);
      setErr(null);
      setLastRefresh(new Date());
    } catch (e) {
      setErr(e?.message || "fetch failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, [refresh]);

  const brainsOnV3 = new Set((status?.brains_on_v3 || []).map((b) => b.toLowerCase()));
  const patientRow = (styles?.styles || []).find(
    (s) => s.execution_style === "PATIENT",
  );
  const patientTrades = patientRow?.trades || 0;
  const v3PatientCandidates = (retirement?.candidates || []).filter(
    (c) => c.scope === "v3_patient_only",
  ).length;
  const judgeState = classifyJudgeState(styles, v3PatientCandidates);

  return (
    <div
      className="border border-rd-border bg-rd-bg p-3 space-y-3"
      data-testid="paradox-v3-rollout-tile"
    >
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-rd-dim">
            Paradox V3 Rollout
          </div>
          <div className="font-mono text-xs text-rd-text mt-0.5">
            {status?.rollout_step?.replace(/_/g, " ") || "—"}
          </div>
        </div>
        <button
          onClick={refresh}
          className="text-rd-dim hover:text-rd-text"
          data-testid="v3-tile-refresh"
          title="Refresh now"
        >
          <ArrowsClockwise size={14} />
        </button>
      </div>

      {err && (
        <div
          className="flex items-center gap-1.5 text-[10px] text-amber-500"
          data-testid="v3-tile-error"
        >
          <Warning size={12} />
          <span>{err}</span>
        </div>
      )}

      {loading && !status && (
        <div className="text-rd-dim text-[10px] py-2">Loading…</div>
      )}

      {!loading && status && (
        <>
          {/* Brains row */}
          <div data-testid="v3-tile-brains">
            <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim mb-1">
              Brains on v3
            </div>
            <div className="flex flex-wrap gap-3">
              {ALL_BRAINS.map((b) => {
                const on = brainsOnV3.has(b);
                return (
                  <div
                    key={b}
                    className="flex items-center gap-1 font-mono text-[11px]"
                    data-testid={`v3-tile-brain-${b}`}
                  >
                    {on
                      ? <CheckCircle size={12} weight="fill" color="#10B981" />
                      : <Circle size={12} color="#6B7280" />}
                    <span className={on ? "text-rd-text" : "text-rd-dim"}>
                      {b}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Patient outcomes progress */}
          <div data-testid="v3-tile-patient-progress">
            <div className="flex items-center justify-between font-mono text-[9px] uppercase tracking-widest text-rd-dim mb-1">
              <span>Patient outcomes</span>
              <span className="text-rd-text">
                {patientTrades} / {READY_THRESHOLD}
              </span>
            </div>
            <ProgressBar value={patientTrades} max={READY_THRESHOLD} />
          </div>

          {/* Per-style table */}
          {(styles?.styles || []).length > 0 && (
            <div data-testid="v3-tile-styles-table">
              <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim mb-1">
                Execution Style Outcomes
              </div>
              <table className="w-full font-mono text-[10px] border-collapse">
                <thead>
                  <tr className="text-rd-dim text-left">
                    <th className="py-1 pr-2">Style</th>
                    <th className="py-1 pr-2 text-right">Trades</th>
                    <th className="py-1 pr-2 text-right">Win%</th>
                    <th className="py-1 pr-2 text-right">Avg PnL</th>
                    <th className="py-1 text-right">State</th>
                  </tr>
                </thead>
                <tbody>
                  {styles.styles.map((row) => (
                    <tr
                      key={row.execution_style}
                      className="border-t border-rd-border/30"
                      data-testid={`v3-tile-style-row-${row.execution_style.toLowerCase()}`}
                    >
                      <td className="py-1 pr-2 text-rd-text">{row.execution_style}</td>
                      <td className="py-1 pr-2 text-right text-rd-text">{row.trades}</td>
                      <td className="py-1 pr-2 text-right text-rd-text">
                        {row.win_rate !== null ? `${(row.win_rate * 100).toFixed(0)}%` : "—"}
                      </td>
                      <td
                        className="py-1 pr-2 text-right"
                        style={{ color: row.avg_pnl_usd >= 0 ? "#10B981" : "#EF4444" }}
                      >
                        {row.avg_pnl_usd >= 0 ? "+" : ""}{row.avg_pnl_usd.toFixed(2)}
                      </td>
                      <td className="py-1 text-right">
                        <StateBadge state={row.state} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Footer summary */}
          <div className="grid grid-cols-3 gap-2 pt-1 border-t border-rd-border/30">
            <div data-testid="v3-tile-judge-state">
              <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim">
                Execution Judge
              </div>
              <div className="font-mono text-[11px] mt-0.5">
                <span
                  style={{
                    color: judgeState === "TRIPPED" ? "#EF4444" :
                           judgeState === "READY"   ? "#10B981" : "#F59E0B",
                  }}
                >
                  {judgeState}
                </span>
              </div>
            </div>
            <div data-testid="v3-tile-retirement-count">
              <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim">
                Retirement candidates
              </div>
              <div className="font-mono text-[11px] mt-0.5 text-rd-text">
                {v3PatientCandidates}
              </div>
            </div>
            <div data-testid="v3-tile-refire-state">
              <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim">
                Watcher / Refire
              </div>
              <div className="font-mono text-[11px] mt-0.5 text-rd-text">
                {status.trigger_watcher_enabled ? "ON" : "off"}
                {" / "}
                {status.trigger_refire_enabled ? "ON" : "off"}
              </div>
            </div>
          </div>

          {lastRefresh && (
            <div className="font-mono text-[9px] text-rd-dim text-right">
              refreshed {lastRefresh.toLocaleTimeString()}
            </div>
          )}
        </>
      )}
    </div>
  );
}
