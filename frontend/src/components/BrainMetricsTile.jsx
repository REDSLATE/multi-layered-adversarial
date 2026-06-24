/**
 * BrainMetricsTile — five operator-tracked KPIs for the multi-day
 * observation window (2026-02).
 *
 * Operator pin: "We need to track these over the next few days:
 *  HOLD count, Entropy average, Reason-code distribution,
 *  Lane-specific decisions, Probability spread."
 *
 * Polls GET /api/admin/brain-metrics?hours=24 every 60s.
 * Also fetches /history to render a sparkline of mean entropy + mean
 * probability-spread over the last 72h so trend is visible at a glance.
 *
 * READ-ONLY. No toggles, no actions.
 */
import { useCallback, useEffect, useState } from "react";
import { ArrowsClockwise, ChartLineUp, XCircle } from "@phosphor-icons/react";
import { api } from "@/lib/api";

const POLL_MS = 60_000;
const WINDOWS = [1, 6, 24, 72];


function Sparkline({ values, color = "#3B82F6", height = 18, width = 80 }) {
  if (!values || values.length < 2) {
    return <span className="text-rd-dim text-[9px]">—</span>;
  }
  const nums = values.filter((v) => typeof v === "number");
  if (nums.length < 2) return <span className="text-rd-dim text-[9px]">—</span>;
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const range = Math.max(max - min, 1e-9);
  const points = nums
    .map((v, i) => {
      const x = (i / (nums.length - 1)) * width;
      const y = height - ((v - min) / range) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg width={width} height={height} className="inline-block align-middle">
      <polyline
        fill="none"
        stroke={color}
        strokeWidth="1.25"
        points={points}
      />
    </svg>
  );
}


function MetricCard({ label, value, sub, sparkline, testid }) {
  return (
    <div
      className="border border-rd-border bg-rd-bg p-2 space-y-0.5"
      data-testid={testid}
    >
      <div className="flex items-center justify-between">
        <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim">
          {label}
        </div>
        {sparkline}
      </div>
      <div className="font-mono text-lg text-rd-text font-bold">{value}</div>
      {sub && (
        <div className="font-mono text-[9px] text-rd-dim">{sub}</div>
      )}
    </div>
  );
}


export default function BrainMetricsTile() {
  const [hours, setHours] = useState(24);
  const [data, setData] = useState(null);
  const [history, setHistory] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (h) => {
    setLoading(true);
    try {
      const [cur, hist] = await Promise.all([
        api.get(`/admin/brain-metrics?hours=${h}`),
        api.get(`/admin/brain-metrics/history?hours=72&window_hours=${h}`),
      ]);
      setData(cur.data);
      setHistory(hist.data);
      setErr(null);
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setErr(typeof d === "string" ? d : JSON.stringify(d));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(hours);
    const id = setInterval(() => load(hours), POLL_MS);
    return () => clearInterval(id);
  }, [load, hours]);

  // Extract sparkline series from the history snapshots.
  const entropySeries = (history?.snapshots || [])
    .map((s) => s.entropy_mean)
    .filter((v) => typeof v === "number");
  const spreadSeries = (history?.snapshots || [])
    .map((s) => s.prob_spread_mean)
    .filter((v) => typeof v === "number");
  const holdSeries = (history?.snapshots || [])
    .map((s) => s.hold_combined)
    .filter((v) => typeof v === "number");
  const consensusSeries = (history?.snapshots || [])
    .map((s) => s.consensus_applied_rate)
    .filter((v) => typeof v === "number");

  const holds = data?.holds;
  const entropy = data?.entropy;
  const reasons = data?.reason_codes;
  const lanes = data?.lane_decisions;
  const spread = data?.probability_spread;
  const consensus = data?.consensus_boost;

  // Operator-pinned color mapping for the consensus health bands.
  //   noise (0-5%)     → dim/yellow  — advisors not aligning
  //   healthy (5-25%)  → green       — sweet spot
  //   heavy (25-50%)   → amber       — leaning on advisors a lot
  //   over_dependent (50%+) → red    — executor too dependent
  const consensusBandColor =
    {
      no_data: "text-rd-dim",
      noise: "text-rd-warn",
      healthy: "text-rd-success",
      heavy: "text-rd-warn",
      over_dependent: "text-rd-danger",
    }[consensus?.health_band] || "text-rd-dim";
  const consensusBandLabel =
    {
      no_data: "no data",
      noise: "noise · advisors not aligning",
      healthy: "healthy · selective influence",
      heavy: "heavy · executor leaning on advisors",
      over_dependent: "over-dependent · executor too reliant",
    }[consensus?.health_band] || "—";

  return (
    <div
      className="border-2 border-rd-accent/60 bg-rd-bg2 p-2.5 space-y-2 mt-4"
      data-testid="brain-metrics-tile"
    >
      <div className="flex items-center gap-2">
        <ChartLineUp size={13} weight="bold" className="text-rd-accent" />
        <div className="flex-1">
          <div
            className="font-mono text-[11px] uppercase tracking-widest text-rd-text font-bold"
            data-testid="brain-metrics-title"
          >
            Brain metrics · multi-day observation
          </div>
          <div className="font-mono text-[9px] text-rd-dim mt-0.5">
            HOLD count · Entropy · Reason codes · Lane decisions · Probability spread
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          {WINDOWS.map((h) => (
            <button
              key={h}
              onClick={() => setHours(h)}
              className={
                "px-2 py-0.5 font-mono text-[10px] uppercase border " +
                (hours === h
                  ? "border-rd-accent text-rd-accent"
                  : "border-rd-border text-rd-dim hover:text-rd-text")
              }
              data-testid={`brain-metrics-window-${h}h`}
            >
              {h}h
            </button>
          ))}
          <button
            onClick={() => load(hours)}
            disabled={loading}
            className="p-1 border border-rd-border text-rd-dim hover:text-rd-text"
            data-testid="brain-metrics-reload"
            title="Reload now"
          >
            <ArrowsClockwise size={11} weight="bold" className={loading ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {err && (
        <div
          className="border border-rd-danger bg-rd-bg p-2 font-mono text-[10px] text-rd-danger flex items-start gap-1.5"
          data-testid="brain-metrics-error"
        >
          <XCircle size={11} weight="bold" className="mt-0.5 shrink-0" />
          {err}
        </div>
      )}

      {data && (
        <>
          {/* Top row: 3 headline KPIs with sparklines */}
          <div className="grid grid-cols-3 gap-2">
            <MetricCard
              label="HOLD count"
              testid="metric-hold-count"
              value={holds?.combined ?? "—"}
              sub={
                holds
                  ? `v2 HOLD: ${holds.v2_hold} · v3 (W/D/A): ${holds.v3_total}`
                  : ""
              }
              sparkline={
                <Sparkline values={holdSeries} color="#FBBF24" />
              }
            />
            <MetricCard
              label="Entropy mean"
              testid="metric-entropy"
              value={
                entropy?.mean_across_brains !== null && entropy?.mean_across_brains !== undefined
                  ? entropy.mean_across_brains.toFixed(3)
                  : "—"
              }
              sub={
                entropy
                  ? `${Object.keys(entropy.per_brain || {}).length} brain(s) · K=${entropy.global_action_cardinality}`
                  : ""
              }
              sparkline={
                <Sparkline values={entropySeries} color="#3B82F6" />
              }
            />
            <MetricCard
              label="Prob spread mean"
              testid="metric-prob-spread"
              value={
                spread?.mean_spread !== null && spread?.mean_spread !== undefined
                  ? spread.mean_spread.toFixed(3)
                  : "—"
              }
              sub={
                spread
                  ? `${spread.n_disagreement_buckets} disagreement bucket(s) · max ${spread.max_spread?.toFixed?.(3) ?? "—"}`
                  : ""
              }
              sparkline={
                <Sparkline values={spreadSeries} color="#A855F7" />
              }
            />
          </div>

          {/* Consensus boost applied rate — operator-pinned KPI (2026-06-24) */}
          <div
            className="border-2 border-rd-border bg-rd-bg p-2"
            data-testid="metric-consensus-applied-rate"
          >
            <div className="flex items-center justify-between">
              <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim">
                Consensus boost applied rate
              </div>
              <Sparkline values={consensusSeries} color="#22D3EE" />
            </div>
            <div className="flex items-baseline gap-2 mt-0.5">
              <div
                className={`font-mono text-lg font-bold ${consensusBandColor}`}
                data-testid="metric-consensus-rate-value"
              >
                {consensus?.applied_rate !== null && consensus?.applied_rate !== undefined
                  ? `${(consensus.applied_rate * 100).toFixed(1)}%`
                  : "—"}
              </div>
              <div
                className={`font-mono text-[10px] uppercase ${consensusBandColor}`}
                data-testid="metric-consensus-band"
              >
                {consensusBandLabel}
              </div>
            </div>
            <div className="font-mono text-[9px] text-rd-dim mt-0.5">
              {consensus
                ? `${consensus.applied_count}/${consensus.total_evaluated} executor evals · +boost ${consensus.positive_boost_count} · −boost ${consensus.negative_boost_count}`
                : "—"}
            </div>
            <div className="font-mono text-[9px] text-rd-dim mt-0.5 opacity-70">
              Bands: 0-5% noise · 5-25% healthy · 25-50% heavy · 50%+ over-dependent
            </div>
          </div>

          {/* Lane-specific decisions */}
          {lanes && Object.keys(lanes).length > 0 && (
            <div className="border border-rd-border bg-rd-bg p-2" data-testid="metric-lane-decisions">
              <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim mb-1">
                Lane decisions
              </div>
              <div className="grid grid-cols-2 gap-2">
                {Object.entries(lanes).map(([lane, counts]) => (
                  <div key={lane} className="border border-rd-border p-1.5" data-testid={`lane-${lane}`}>
                    <div className="font-mono text-[10px] uppercase font-bold text-rd-text">
                      {lane}{" "}
                      <span className="text-rd-dim font-normal">
                        ({counts.total ?? 0})
                      </span>
                    </div>
                    <div className="font-mono text-[9px] text-rd-dim mt-0.5 flex flex-wrap gap-x-2">
                      {Object.entries(counts)
                        .filter(([k]) => k !== "total")
                        .sort(([, a], [, b]) => b - a)
                        .map(([action, n]) => (
                          <span key={action}>
                            <span className="text-rd-text">{action}</span>: {n}
                          </span>
                        ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Reason-code distribution */}
          {reasons && (
            <div className="grid grid-cols-2 gap-2">
              <div className="border border-rd-border bg-rd-bg p-2" data-testid="metric-gate-states">
                <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim mb-1">
                  Top gate states
                </div>
                {reasons.top_gate_states?.length > 0 ? (
                  <div className="space-y-0.5">
                    {reasons.top_gate_states.slice(0, 8).map((r) => (
                      <div key={r.reason} className="flex justify-between font-mono text-[10px]">
                        <span className="text-rd-text truncate pr-2">{r.reason}</span>
                        <span className="text-rd-dim shrink-0">
                          {r.count}{" "}
                          <span className="text-rd-dim">({r.pct_of_total.toFixed(1)}%)</span>
                        </span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="text-rd-dim font-mono text-[10px]">no intents in window</div>
                )}
              </div>
              <div className="border border-rd-border bg-rd-bg p-2" data-testid="metric-final-reasons">
                <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim mb-1">
                  Top final reasons
                </div>
                {reasons.top_final_reasons?.length > 0 ? (
                  <div className="space-y-0.5">
                    {reasons.top_final_reasons.slice(0, 8).map((r) => (
                      <div key={r.reason} className="flex justify-between font-mono text-[10px]">
                        <span className="text-rd-text truncate pr-2">{r.reason}</span>
                        <span className="text-rd-dim shrink-0">
                          {r.count}{" "}
                          <span className="text-rd-dim">({r.pct_of_total.toFixed(1)}%)</span>
                        </span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="text-rd-dim font-mono text-[10px]">no pipeline receipts in window</div>
                )}
              </div>
            </div>
          )}

          {/* Per-brain entropy detail (collapsible) */}
          {entropy?.per_brain && Object.keys(entropy.per_brain).length > 0 && (
            <details className="font-mono text-[10px]">
              <summary className="cursor-pointer text-rd-dim hover:text-rd-text">
                Per-brain entropy ▾
              </summary>
              <div className="grid grid-cols-2 gap-2 mt-1" data-testid="metric-per-brain-entropy">
                {Object.entries(entropy.per_brain).map(([brain, info]) => (
                  <div key={brain} className="border border-rd-border p-1.5">
                    <div className="text-rd-text font-bold uppercase">
                      {brain}{" "}
                      <span className="text-rd-dim font-normal">
                        H={info.entropy.toFixed(3)} · n={info.n_intents}
                      </span>
                    </div>
                    <div className="text-rd-dim text-[9px] mt-0.5">
                      {Object.entries(info.distribution || {})
                        .sort(([, a], [, b]) => b - a)
                        .map(([a, n]) => `${a}:${n}`)
                        .join(" · ")}
                    </div>
                  </div>
                ))}
              </div>
            </details>
          )}

          {/* Top probability-disagreement (collapsible) */}
          {spread?.top_disagreement?.length > 0 && (
            <details className="font-mono text-[10px]">
              <summary className="cursor-pointer text-rd-dim hover:text-rd-text">
                Top probability-disagreement buckets ▾
              </summary>
              <div className="mt-1 space-y-0.5" data-testid="metric-top-disagreement">
                {spread.top_disagreement.map((d, i) => (
                  <div
                    key={`${d.symbol}-${d.ts_bucket}-${i}`}
                    className="border border-rd-border p-1.5"
                  >
                    <div className="flex justify-between">
                      <span className="text-rd-text font-bold">{d.symbol}</span>
                      <span className="text-rd-warn">spread {d.spread.toFixed(3)}</span>
                    </div>
                    <div className="text-rd-dim text-[9px]">
                      {d.ts_bucket} ·{" "}
                      {Object.entries(d.brains).map(([b, c]) => `${b}:${c.toFixed(2)}`).join(" · ")}
                    </div>
                  </div>
                ))}
              </div>
            </details>
          )}

          <div className="font-mono text-[9px] text-rd-dim border-t border-rd-border pt-1">
            Window: {data.window_hours}h · intents: {data.total_intents} ·
            snapshots: {history?.n_snapshots ?? 0} (last 72h) ·
            fetched {data.fetched_at?.split("T")[1]?.slice(0, 5) || ""}Z
          </div>
        </>
      )}
    </div>
  );
}
