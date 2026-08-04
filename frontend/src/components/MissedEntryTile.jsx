import React, { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Crosshair } from "@phosphor-icons/react";

const OUTCOME_STYLE = {
  tp_hit: "text-rd-success border-rd-success",
  sl_hit: "text-red-500 border-red-500",
  expired: "text-rd-muted border-rd-border",
  no_data: "text-rd-dim border-rd-border",
};

/** Missed-Entry Ledger — counterfactual outcomes for blocked BUYs.
 *  Evidence for tuning gates instead of tuning blind. Observe-only. */
export const MissedEntryTile = () => {
  const [data, setData] = useState(null);
  const [alerts, setAlerts] = useState(null);
  const [acking, setAcking] = useState(false);

  const load = () => {
    api.get("/admin/missed-entries?hours=168").then(({ data: d }) => setData(d)).catch(() => {});
    api.get("/admin/alerts?hours=168").then(({ data: a }) => setAlerts(a)).catch(() => {});
  };

  const ack = async (payload) => {
    setAcking(true);
    try {
      await api.post("/admin/alerts/ack", payload);
      load();
    } finally {
      setAcking(false);
    }
  };

  useEffect(() => {
    load();
    const t = setInterval(load, 120000);
    return () => clearInterval(t);
  }, []);

  const reasons = Object.entries(data?.by_reason || {});
  const recent = (data?.recent || []).filter((r) => r.outcome !== "no_data").slice(0, 6);
  const total = reasons.reduce((a, [, v]) => a + v.n, 0);
  const wouldTp = reasons.reduce((a, [, v]) => a + v.would_tp, 0);
  const liveAlerts = (alerts?.alerts || []).slice(0, 4);

  return (
    <div className="border border-rd-border bg-rd-panel p-3" data-testid="missed-entry-tile">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <Crosshair size={14} weight="bold" className="text-amber-500" />
          <span className="text-xs font-mono font-bold uppercase tracking-widest text-rd-text">
            Missed Entry Ledger
          </span>
        </div>
        <span className="text-[9px] font-mono uppercase text-rd-dim">7d · observe-only</span>
      </div>
      {liveAlerts.length > 0 && (
        <div className="border border-red-500 bg-red-500/10 p-2 mb-2" data-testid="missed-entry-alerts">
          <div className="flex items-center justify-between mb-1">
            <span className="text-[10px] font-mono font-bold uppercase tracking-widest text-red-500 animate-pulse">
              Costly Miss · {alerts?.unacked_total ?? liveAlerts.length} unacked
            </span>
            <button
              onClick={() => ack({ all: true })}
              disabled={acking}
              className="px-2 py-0.5 text-[9px] font-mono uppercase border border-red-500 text-red-500 hover:bg-red-500/20 transition-colors disabled:opacity-40"
              data-testid="missed-entry-ack-all"
            >
              ack all
            </button>
          </div>
          {liveAlerts.map((a) => (
            <div key={a._id} className="flex items-center gap-2 text-[10px] font-mono text-rd-text py-0.5" data-testid={`missed-entry-alert-${a._id}`}>
              <span className="flex-1 truncate" title={a.message}>{a.message}</span>
              <button
                onClick={() => ack({ alert_id: a._id })}
                disabled={acking}
                className="text-rd-dim hover:text-red-500 text-[9px] uppercase transition-colors"
                data-testid={`missed-entry-ack-${a._id}`}
              >
                ack
              </button>
            </div>
          ))}
        </div>
      )}
      {!data ? (
        <div className="text-[10px] font-mono text-rd-dim">loading…</div>
      ) : total === 0 ? (
        <div className="text-[10px] font-mono text-rd-dim" data-testid="missed-entry-empty">
          no blocked BUYs evaluated yet — counterfactuals land {data.config?.horizon_h ?? 4}h after each block
        </div>
      ) : (
        <>
          <div className="flex gap-4 mb-2" data-testid="missed-entry-summary">
            <div className="border border-rd-border px-3 py-1.5">
              <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">blocked BUYs</div>
              <div className="font-display text-lg font-bold text-rd-text">{total}</div>
            </div>
            <div className="border border-rd-border px-3 py-1.5">
              <div className="text-[9px] font-mono uppercase tracking-widest text-rd-dim">would-be TP</div>
              <div className="font-display text-lg font-bold text-amber-500">
                {total > 0 ? Math.round((wouldTp / total) * 100) : 0}%
              </div>
            </div>
            <div className="text-[10px] font-mono text-rd-dim self-end pb-1">
              what blocked trades would have done vs real exit levels
            </div>
          </div>
          <div className="space-y-0.5 mb-2" data-testid="missed-entry-reasons">
            {reasons.map(([reason, v]) => (
              <div key={reason} className="flex gap-3 text-[10px] font-mono items-center">
                <span className="w-64 shrink-0 truncate text-rd-text" title={reason}>{reason}</span>
                <span className="w-10 text-rd-muted">×{v.n}</span>
                <span className="w-24 text-rd-success">peak {v.avg_peak_pct > 0 ? "+" : ""}{v.avg_peak_pct}%</span>
                <span className={`w-24 ${v.avg_end_pct >= 0 ? "text-rd-success" : "text-red-500"}`}>
                  end {v.avg_end_pct > 0 ? "+" : ""}{v.avg_end_pct}%
                </span>
                <span className="text-rd-dim">tp {v.would_tp} · sl {v.would_sl}</span>
              </div>
            ))}
          </div>
          {recent.length > 0 && (
            <div className="border-t border-rd-border pt-1.5 space-y-0.5" data-testid="missed-entry-recent">
              {recent.map((r) => (
                <div key={r.intent_id} className="flex gap-2 text-[10px] font-mono items-center">
                  <span className="w-20 shrink-0 text-rd-text">{r.symbol}</span>
                  <span
                    className={`px-1 border text-[8px] uppercase ${OUTCOME_STYLE[r.outcome] || OUTCOME_STYLE.expired}`}
                    data-testid={`missed-entry-outcome-${r.intent_id}`}
                  >
                    {r.outcome}{r.ambiguous ? "?" : ""}
                  </span>
                  <span className="text-rd-success">peak {r.peak_pct > 0 ? "+" : ""}{r.peak_pct}%</span>
                  <span className={r.end_pct >= 0 ? "text-rd-success" : "text-red-500"}>
                    end {r.end_pct > 0 ? "+" : ""}{r.end_pct}%
                  </span>
                  <span className="ml-auto text-rd-dim truncate max-w-[220px]" title={r.block_reason}>
                    {r.block_reason}
                  </span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
};

export default MissedEntryTile;
