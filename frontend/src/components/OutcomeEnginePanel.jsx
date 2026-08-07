import React, { useState } from "react";
import { api } from "@/lib/api";
import { Crosshair } from "@phosphor-icons/react";

const pct = (v) => (v == null ? "—" : `${(Number(v) * 100).toFixed(2)}%`);

/** Signal Outcome + Attribution — was the signal good, independent of
 *  whether the pipeline executed it well? (triple-barrier engine) */
export const OutcomeEnginePanel = () => {
  const [rollup, setRollup] = useState(null);
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    setBusy(true);
    try {
      await api.post("/admin/outcomes/resolve?limit=200");
      const [{ data }, { data: st }] = await Promise.all([
        api.get("/admin/outcomes/rollup"),
        api.get("/admin/outcomes/status"),
      ]);
      setRollup(data);
      setStatus(st);
    } catch (e) {
      setRollup({ error: e?.response?.data?.detail || e.message });
    } finally { setBusy(false); }
  };

  return (
    <div className="border border-rd-border bg-rd-panel p-3 mb-4" data-testid="outcome-engine-panel">
      <div className="flex items-center justify-between mb-1.5">
        <div className="flex items-center gap-2">
          <Crosshair size={14} weight="bold" className="text-emerald-400" />
          <span className="text-xs font-mono font-bold uppercase tracking-widest text-rd-text">
            Signal Outcomes · Attribution
          </span>
          <span className="text-[9px] font-mono uppercase text-rd-dim">triple barrier · signal vs execution · advisory only</span>
        </div>
        <button
          onClick={run}
          disabled={busy}
          className="px-3 py-1 text-[10px] font-mono uppercase tracking-wider border border-emerald-400 text-emerald-400 hover:bg-emerald-400/10 transition-colors disabled:opacity-40"
          data-testid="outcome-engine-run-btn"
        >
          {busy ? "resolving…" : "resolve + rollup"}
        </button>
      </div>
      {!rollup ? (
        <div className="text-[10px] font-mono text-rd-dim">
          freezes each signal at birth, replays its own tape against profit/stop/time barriers, then assigns blame: bad signal vs late entry vs gate rejection vs bad exit — feeds brain weighting later
        </div>
      ) : rollup.error ? (
        <div className="text-[10px] font-mono text-red-500" data-testid="outcome-engine-error">{rollup.error}</div>
      ) : (
        <>
          <div className="text-[10px] font-mono text-rd-muted mb-1.5" data-testid="outcome-engine-total">
            {rollup.total} signals resolved
          </div>
          {status && (
            <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-[10px] font-mono mb-2" data-testid="outcome-engine-counters">
              <span className="text-rd-dim">queue: <span className="text-rd-text">{status.eligible_unresolved ?? "—"}</span> eligible unresolved</span>
              <span className="text-rd-dim">last cycle: <span className="text-rd-text">{status.resolved_last_cycle}</span> resolved</span>
              <span className="text-rd-dim">hydrated on boot: <span className="text-rd-text">{status.hydrated_on_boot}</span></span>
              <span className={status.exit_linkage_miss_count > 0 ? "text-amber-500" : "text-rd-dim"}>
                exit-linkage misses: <span className={status.exit_linkage_miss_count > 0 ? "text-amber-500 font-bold" : "text-rd-text"}>{status.exit_linkage_miss_count}</span>
              </span>
            </div>
          )}
          {(rollup.by_attribution || []).length > 0 && (
            <div className="space-y-0.5 mb-2" data-testid="outcome-engine-attributions">
              {rollup.by_attribution.map((a) => (
                <div key={a.attribution} className="flex gap-3 text-[10px] font-mono">
                  <span className={`w-56 shrink-0 ${a.attribution === "BAD_SIGNAL" ? "text-red-500" : a.attribution.startsWith("GOOD_SIGNAL") ? "text-amber-500" : a.attribution === "GOOD_COMPLETE_TRADE" ? "text-rd-success" : "text-rd-text"}`}>
                    {a.attribution.replaceAll("_", " ").toLowerCase()}
                  </span>
                  <span className="w-12 text-rd-muted">×{a.n}</span>
                  <span className="text-rd-dim">theo {pct(a.avg_theoretical)} · actual {pct(a.avg_actual)}</span>
                </div>
              ))}
            </div>
          )}
          {(rollup.by_brain || []).length > 0 && (
            <div className="border-t border-rd-border pt-1.5 space-y-0.5" data-testid="outcome-engine-brains">
              <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim mb-0.5">per brain</div>
              {rollup.by_brain.map((b) => (
                <div key={`${b.brain}-${b.lane}`} className="flex flex-wrap gap-x-3 text-[10px] font-mono">
                  <span className="w-24 shrink-0 text-rd-text">{b.brain} · {b.lane}</span>
                  <span className="text-rd-muted">×{b.n}</span>
                  <span className="text-rd-success">signal wins {b.signal_wins}</span>
                  <span className="text-red-500">bad {b.bad_signals}</span>
                  <span className="text-amber-500">lost by pipeline {b.good_signals_lost_by_pipeline}</span>
                  <span className={b.signal_execution_gap > 0.1 ? "text-red-500" : "text-rd-dim"}>
                    gap {b.signal_execution_gap ?? "—"}
                  </span>
                  <span className="text-rd-dim">capture {b.avg_edge_capture ?? "—"} · delay {b.avg_entry_delay_s ?? "—"}s · {b.sample_state}</span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
};

export default OutcomeEnginePanel;
