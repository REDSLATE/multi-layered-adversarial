/**
 * PerBrainExecutionStyleProfileTile — observational profile of
 * v3 outcomes broken out by brain × execution_style (operator pin
 * 2026-02-23).
 *
 * READ-ONLY. Polls /api/admin/paradox-v3/per-brain-execution-style-
 * profile every 15s and renders a matrix:
 *
 *   rows    = brains (camino, barracuda, hellcat, gto, ...)
 *   columns = execution styles (MARKET_NOW, PATIENT, ...)
 *   cells   = trades, win%, avg PnL, conservative band
 *
 * Doctrine framing (intents.py:696 seat-doctrinal pin):
 *   `stack` is METADATA, not a brain-scoring axis. Cells show
 *   outcomes WHILE a brain occupied the seat. The tile subtitle
 *   explicitly carries this caveat so an operator reading it for
 *   the first time doesn't mistake correlations for scoring.
 *
 * Conservative bands:
 *   LEARNING ≥ 30, READY ≥ 50, STRONG ≥ 100, HIGH_CONVICTION ≥ 200
 *   Anything below 30 → INSUFFICIENT (dim).
 */
import { useCallback, useEffect, useState } from "react";
import { ArrowsClockwise, Warning, Info } from "@phosphor-icons/react";
import { api } from "@/lib/api";

const POLL_MS = 15_000;

const BAND_STYLES = {
  HIGH_CONVICTION: { fg: "#10B981", label: "HIGH" },
  STRONG:          { fg: "#10B981", label: "STRONG" },
  READY:           { fg: "#F59E0B", label: "READY" },
  LEARNING:        { fg: "#6B7280", label: "LEARNING" },
  INSUFFICIENT:    { fg: "#374151", label: "—" },
};


function StateBadge({ state }) {
  const cfg = BAND_STYLES[state] || BAND_STYLES.INSUFFICIENT;
  return (
    <span
      className="font-mono text-[8px] uppercase tracking-widest px-1 py-px border"
      style={{ color: cfg.fg, borderColor: cfg.fg + "55" }}
      data-testid={`pbesp-band-${state.toLowerCase()}`}
    >
      {cfg.label}
    </span>
  );
}


function Cell({ cell }) {
  if (!cell || cell.trades === 0) {
    return (
      <td
        className="py-1.5 px-2 text-center text-rd-dim font-mono text-[10px] border-t border-l border-rd-border/30"
        data-testid="pbesp-cell-empty"
      >
        —
      </td>
    );
  }
  const wr = cell.win_rate !== null
    ? `${(cell.win_rate * 100).toFixed(0)}%`
    : "—";
  const pnlColor = cell.avg_pnl_usd >= 0 ? "#10B981" : "#EF4444";
  return (
    <td
      className="py-1.5 px-2 text-right border-t border-l border-rd-border/30 align-top"
      data-testid={`pbesp-cell-${cell.brain}-${cell.execution_style.toLowerCase()}`}
    >
      <div className="flex items-center justify-end gap-1.5 font-mono text-[10px]">
        <span className="text-rd-text">{cell.trades}</span>
        <span className="text-rd-dim">·</span>
        <span className="text-rd-text">{wr}</span>
      </div>
      <div
        className="font-mono text-[9px] text-right"
        style={{ color: pnlColor }}
      >
        {cell.avg_pnl_usd >= 0 ? "+" : ""}{cell.avg_pnl_usd.toFixed(2)}
      </div>
      <div className="text-right mt-0.5">
        <StateBadge state={cell.state} />
      </div>
    </td>
  );
}


function TotalCell({ total }) {
  if (!total || total.trades === 0) {
    return (
      <td className="py-1.5 px-2 text-rd-dim font-mono text-[10px] border-t border-l border-rd-border/30">
        —
      </td>
    );
  }
  const wr = total.win_rate !== null
    ? `${(total.win_rate * 100).toFixed(0)}%`
    : "—";
  const pnlColor = total.avg_pnl_usd >= 0 ? "#10B981" : "#EF4444";
  return (
    <td
      className="py-1.5 px-2 text-right border-t border-l border-rd-border/30 align-top bg-rd-bg/40"
      data-testid={`pbesp-total-${total.brain}`}
    >
      <div className="flex items-center justify-end gap-1.5 font-mono text-[10px]">
        <span className="text-rd-text font-semibold">{total.trades}</span>
        <span className="text-rd-dim">·</span>
        <span className="text-rd-text">{wr}</span>
      </div>
      <div
        className="font-mono text-[9px] text-right"
        style={{ color: pnlColor }}
      >
        {total.avg_pnl_usd >= 0 ? "+" : ""}{total.avg_pnl_usd.toFixed(2)}
      </div>
      <div className="text-right mt-0.5">
        <StateBadge state={total.state} />
      </div>
    </td>
  );
}


export default function PerBrainExecutionStyleProfileTile() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const [lastRefresh, setLastRefresh] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const out = await api.get(
        "/admin/paradox-v3/per-brain-execution-style-profile",
      );
      setData(out);
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

  // Build a fast lookup: cellMap[brain][style] -> cell
  const cellMap = {};
  (data?.cells || []).forEach((c) => {
    if (!cellMap[c.brain]) cellMap[c.brain] = {};
    cellMap[c.brain][c.execution_style] = c;
  });
  const totalMap = {};
  (data?.totals_by_brain || []).forEach((t) => { totalMap[t.brain] = t; });

  const brains = data?.brains || [];
  const styles = data?.styles || [];
  const hasData = brains.length > 0 && styles.length > 0;

  return (
    <div
      className="border border-rd-border bg-rd-bg p-3 space-y-3"
      data-testid="per-brain-execution-style-profile-tile"
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-rd-dim">
            Per-Brain · Execution-Style Profile
          </div>
          <div className="font-mono text-[10px] text-rd-dim mt-0.5 flex items-center gap-1">
            <Info size={10} />
            <span data-testid="pbesp-observational-caveat">
              OBSERVATIONAL — stack is metadata, not a brain-scoring axis
            </span>
          </div>
        </div>
        <button
          onClick={refresh}
          className="text-rd-dim hover:text-rd-text"
          data-testid="pbesp-refresh"
          title="Refresh now"
        >
          <ArrowsClockwise size={14} />
        </button>
      </div>

      {err && (
        <div
          className="flex items-center gap-1.5 text-[10px] text-amber-500"
          data-testid="pbesp-error"
        >
          <Warning size={12} />
          <span>{err}</span>
        </div>
      )}

      {loading && !data && (
        <div className="text-rd-dim text-[10px] py-2">Loading…</div>
      )}

      {!loading && data && !hasData && (
        <div
          className="text-rd-dim text-[10px] py-3"
          data-testid="pbesp-empty"
        >
          No v3 outcomes joined yet. The matrix populates once
          `intent_version=v3` rows accumulate resolved `outcome_join`
          records.
        </div>
      )}

      {!loading && data && hasData && (
        <div className="overflow-x-auto" data-testid="pbesp-matrix">
          <table className="w-full font-mono text-[10px] border-collapse">
            <thead>
              <tr className="text-rd-dim text-left">
                <th className="py-1.5 px-2 font-mono text-[9px] uppercase tracking-widest">
                  Brain
                </th>
                {styles.map((s) => (
                  <th
                    key={s}
                    className="py-1.5 px-2 text-right font-mono text-[9px] uppercase tracking-widest border-l border-rd-border/30"
                    data-testid={`pbesp-col-${s.toLowerCase()}`}
                  >
                    {s.replace(/_/g, " ")}
                  </th>
                ))}
                <th className="py-1.5 px-2 text-right font-mono text-[9px] uppercase tracking-widest border-l border-rd-border/30 bg-rd-bg/40">
                  Σ Total
                </th>
              </tr>
            </thead>
            <tbody>
              {brains.map((brain) => (
                <tr key={brain} data-testid={`pbesp-row-${brain}`}>
                  <td className="py-1.5 px-2 text-rd-text font-mono text-[10px] border-t border-rd-border/30 align-top">
                    {brain}
                  </td>
                  {styles.map((style) => (
                    <Cell key={style} cell={cellMap[brain]?.[style]} />
                  ))}
                  <TotalCell total={totalMap[brain]} />
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-2 font-mono text-[9px] text-rd-dim leading-relaxed">
            <div>
              Cell legend: <span className="text-rd-text">trades · win%</span>
              <span className="ml-2">avg PnL (green ≥0)</span>
            </div>
            <div className="mt-0.5">
              Bands: LEARNING ≥ 30 · READY ≥ 50 · STRONG ≥ 100 ·
              HIGH ≥ 200. Below 30 → INSUFFICIENT.
            </div>
          </div>
        </div>
      )}

      {lastRefresh && (
        <div className="font-mono text-[9px] text-rd-dim text-right">
          refreshed {lastRefresh.toLocaleTimeString()}
        </div>
      )}
    </div>
  );
}
