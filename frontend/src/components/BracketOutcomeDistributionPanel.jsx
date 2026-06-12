import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card } from "@/components/ui-bits";
import { Target, ArrowsClockwise } from "@phosphor-icons/react";

/**
 * BracketOutcomeDistributionPanel — training-signal quality tile.
 *
 * Surfaces the live conversion rate of brain intents into clean
 * categorical outcomes (tp_hit / sl_hit / timeout). The whole point:
 * if confidence is well calibrated, the `tp_rate` per confidence
 * band should monotonically increase as confidence rises.
 *
 * Doctrine read:
 *   • Master-gated on RISEDUAL_BRACKET_OUTCOMES_ENABLED (default off).
 *     When off, the panel still renders but shows zeros + a hint to
 *     enable the feature.
 *   • The bracket recorder writes only when the brain's intent
 *     carries both target_price + stop_price.
 *   • Brains that don't publish brackets fall back to the legacy
 *     PnL-thresholded outcome resolver — those intents won't appear
 *     in this tile.
 */
export default function BracketOutcomeDistributionPanel() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [windowH, setWindowH] = useState(24);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.get(
        `/admin/brackets/distribution?window_hours=${windowH}`,
      );
      setData(res.data);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  }, [windowH]);

  useEffect(() => {
    load();
  }, [load]);

  if (err) {
    return (
      <Card testid="bracket-outcomes-error">
        <div className="p-4 text-sm text-red-400" data-testid="bracket-outcomes-error-msg">
          bracket distribution unavailable: {err}
        </div>
      </Card>
    );
  }
  if (!data) {
    return (
      <Card testid="bracket-outcomes-loading">
        <div className="p-4 text-sm opacity-60">loading bracket outcomes…</div>
      </Card>
    );
  }

  const labelBg = {
    tp_hit: "text-emerald-300 border-emerald-700/40 bg-emerald-950/30",
    sl_hit: "text-red-300 border-red-700/40 bg-red-950/30",
    timeout: "text-amber-300 border-amber-700/40 bg-amber-950/30",
  };
  const totalResolved = data.total_resolved || 0;
  const tpFrac =
    totalResolved > 0
      ? ((data.by_label.tp_hit / totalResolved) * 100).toFixed(1)
      : "0.0";
  const slFrac =
    totalResolved > 0
      ? ((data.by_label.sl_hit / totalResolved) * 100).toFixed(1)
      : "0.0";
  const timeoutFrac =
    totalResolved > 0
      ? ((data.by_label.timeout / totalResolved) * 100).toFixed(1)
      : "0.0";

  const binOrder = ["0.0-0.3", "0.3-0.5", "0.5-0.7", "0.7-0.85", "0.85-1.0"];

  return (
    <Card testid="bracket-outcomes-panel">
      <div className="p-4 space-y-4" data-testid="bracket-outcomes-content">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Target size={18} className="opacity-70" />
            <div className="text-xs uppercase tracking-wider opacity-60">
              Training Signal · Bracket Outcomes
            </div>
          </div>
          <div className="flex items-center gap-2">
            <select
              data-testid="bracket-window-select"
              className="bg-transparent border border-zinc-700 rounded px-2 py-0.5 text-xs"
              value={windowH}
              onChange={(e) => setWindowH(Number(e.target.value))}
            >
              <option value={1}>1h</option>
              <option value={6}>6h</option>
              <option value={24}>24h</option>
              <option value={72}>72h</option>
              <option value={168}>7d</option>
            </select>
            <button
              data-testid="bracket-reload-btn"
              onClick={load}
              disabled={loading}
              className="text-xs px-2 py-0.5 border border-zinc-700 rounded opacity-80 hover:opacity-100 inline-flex items-center gap-1"
            >
              <ArrowsClockwise size={12} /> reload
            </button>
          </div>
        </div>

        {/* Headline totals */}
        <div className="grid grid-cols-2 gap-3" data-testid="bracket-headline">
          <div className="border border-zinc-800 rounded px-3 py-2">
            <div className="text-[10px] uppercase opacity-50">resolved · window</div>
            <div className="text-2xl font-semibold tabular-nums">
              {totalResolved}
            </div>
          </div>
          <div className="border border-zinc-800 rounded px-3 py-2">
            <div className="text-[10px] uppercase opacity-50">open · live</div>
            <div className="text-2xl font-semibold tabular-nums">
              {data.total_open || 0}
            </div>
          </div>
        </div>

        {/* By-label totals */}
        <div className="grid grid-cols-3 gap-2" data-testid="bracket-by-label">
          <div className={`border rounded px-2 py-1.5 ${labelBg.tp_hit}`} data-testid="bracket-tp-hit-tile">
            <div className="text-[10px] uppercase opacity-70">tp_hit</div>
            <div className="text-lg font-semibold tabular-nums">
              {data.by_label.tp_hit}
              <span className="text-xs opacity-70 ml-1">{tpFrac}%</span>
            </div>
          </div>
          <div className={`border rounded px-2 py-1.5 ${labelBg.sl_hit}`} data-testid="bracket-sl-hit-tile">
            <div className="text-[10px] uppercase opacity-70">sl_hit</div>
            <div className="text-lg font-semibold tabular-nums">
              {data.by_label.sl_hit}
              <span className="text-xs opacity-70 ml-1">{slFrac}%</span>
            </div>
          </div>
          <div className={`border rounded px-2 py-1.5 ${labelBg.timeout}`} data-testid="bracket-timeout-tile">
            <div className="text-[10px] uppercase opacity-70">timeout</div>
            <div className="text-lg font-semibold tabular-nums">
              {data.by_label.timeout}
              <span className="text-xs opacity-70 ml-1">{timeoutFrac}%</span>
            </div>
          </div>
        </div>

        {/* Calibration curve — tp_rate per confidence band. */}
        <div data-testid="bracket-by-conf-bin">
          <div className="text-[10px] uppercase opacity-50 mb-1">
            Confidence calibration · tp_rate by confidence band
          </div>
          <div className="space-y-1">
            {binOrder.map((b) => {
              const row = data.by_confidence_bin?.[b] || {
                tp_hit: 0, sl_hit: 0, timeout: 0, total: 0, tp_rate: null,
              };
              const tpPct =
                row.tp_rate == null ? "—" : (row.tp_rate * 100).toFixed(0) + "%";
              const widthPct = row.tp_rate == null ? 0 : row.tp_rate * 100;
              return (
                <div
                  key={b}
                  className="flex items-center gap-2 text-xs"
                  data-testid={`bracket-bin-${b}`}
                >
                  <div className="w-20 opacity-70 tabular-nums">{b}</div>
                  <div className="flex-1 h-3 bg-zinc-900 rounded relative overflow-hidden">
                    <div
                      className="h-full bg-emerald-700/60"
                      style={{ width: `${widthPct}%` }}
                    />
                  </div>
                  <div className="w-12 text-right tabular-nums opacity-80">
                    {tpPct}
                  </div>
                  <div className="w-16 text-right tabular-nums opacity-50 text-[10px]">
                    n={row.total}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {totalResolved === 0 && (
          <div className="text-xs opacity-60 border-l-2 border-amber-700/50 pl-2" data-testid="bracket-empty-hint">
            No resolved brackets in window. Enable with
            <code className="px-1 mx-1 bg-zinc-900 rounded">RISEDUAL_BRACKET_OUTCOMES_ENABLED=true</code>
            and make sure brains are publishing <code>target_price</code> +
            <code className="ml-1">stop_price</code> on directional intents.
          </div>
        )}

        {data.recent_open && data.recent_open.length > 0 && (
          <details className="text-xs">
            <summary className="opacity-60 cursor-pointer" data-testid="bracket-open-list-toggle">
              {data.recent_open.length} open bracket{data.recent_open.length !== 1 ? "s" : ""} (most recent)
            </summary>
            <table className="mt-2 w-full text-[11px]" data-testid="bracket-open-list">
              <thead className="opacity-60">
                <tr>
                  <th className="text-left font-normal">symbol</th>
                  <th className="text-left font-normal">side</th>
                  <th className="text-right font-normal">entry</th>
                  <th className="text-right font-normal">target</th>
                  <th className="text-right font-normal">stop</th>
                  <th className="text-right font-normal">conf</th>
                </tr>
              </thead>
              <tbody>
                {data.recent_open.slice(0, 10).map((r) => (
                  <tr key={r.bracket_id} className="border-t border-zinc-900">
                    <td className="py-0.5">{r.symbol}</td>
                    <td>{r.side}</td>
                    <td className="text-right tabular-nums">{Number(r.entry_price).toFixed(2)}</td>
                    <td className="text-right tabular-nums text-emerald-300/80">{Number(r.target_price).toFixed(2)}</td>
                    <td className="text-right tabular-nums text-red-300/80">{Number(r.stop_price).toFixed(2)}</td>
                    <td className="text-right tabular-nums">{Number(r.confidence).toFixed(2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        )}

        <div className="text-[10px] opacity-40 leading-snug">
          {data.doctrine_note}
        </div>
      </div>
    </Card>
  );
}
