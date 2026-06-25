/**
 * ExecutionLifecycleFunnelTile — answers "what happened AFTER the
 * broker accepted the order?" at a glance.
 *
 * Operator doctrine (2026-02-23 P3): identity drift is closed,
 * 3-mode authority is enforced, post-mortem classifications are
 * clean. The next operational blind spot is post-acceptance:
 *
 *   accepted → filled / partially_filled / canceled / working / unknown
 *
 * This tile joins shared_intents{executed:True} to broker_orders
 * via broker_order_id, classifies each row through the canonical
 * 5-bucket taxonomy (shared/broker_status_classifier.py), and
 * renders the distribution + per-lane + per-brain breakdown + a
 * sample of UNKNOWN intent_ids so the operator can debug missing
 * receipts in one glance.
 *
 * Read-only. Polls /api/admin/execution-lifecycle/funnel every
 * 30s. Defaults to the 24h window with the equity lane filter
 * pre-selected — matching the current operator debugging plan
 * (crypto disabled until Saturday, equity baseline observation).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowsClockwise, Info } from "@phosphor-icons/react";
import { api } from "@/lib/api";

const POLL_MS = 30_000;
const WINDOWS = [
  { label: "1h",  value: 1 },
  { label: "6h",  value: 6 },
  { label: "24h", value: 24 },
  { label: "72h", value: 72 },
];
const LANE_FILTERS = [
  { label: "All",    value: "" },
  { label: "Equity", value: "equity" },
  { label: "Crypto", value: "crypto" },
];

// Bucket colors — chosen so the operator's eye is drawn to FILLED
// (green = good) vs WORKING/UNKNOWN (amber = needs attention) vs
// CANCELED (slate = cleared) — partial sits between filled and
// working visually.
const BUCKET_COLORS = {
  filled:           "#10B981", // emerald — terminal success
  partially_filled: "#F59E0B", // amber — partial success
  working:          "#3B82F6", // blue — still alive
  canceled:         "#94A3B8", // slate — cleared / refused
  unknown:          "#EF4444", // red — visibility gap
};

const BUCKET_LABELS = {
  filled:           "Filled",
  partially_filled: "Partial",
  working:          "Working",
  canceled:         "Canceled",
  unknown:          "Unknown",
};


function pctText(n) {
  if (n == null || Number.isNaN(n)) return "—";
  return `${n.toFixed(1)}%`;
}


function BucketBar({ counts, percentages, order, total }) {
  // Single-row stacked bar: every bucket is one segment, widths
  // proportional to count. UNKNOWN gets its red color so a
  // visibility gap is impossible to miss even at a glance.
  if (!total || total === 0) {
    return (
      <div className="text-xs text-slate-500 font-mono italic py-2"
           data-testid="lifecycle-funnel-empty">
        No executed intents in this window.
      </div>
    );
  }
  return (
    <div className="space-y-1" data-testid="lifecycle-funnel-bar">
      <div className="flex h-7 w-full border border-slate-700 overflow-hidden">
        {order.map((b) => {
          const c = counts[b] || 0;
          if (!c) return null;
          const w = (c / total) * 100;
          return (
            <div
              key={b}
              className="h-full flex items-center justify-center text-[10px] font-mono font-bold text-white"
              style={{ width: `${w}%`, backgroundColor: BUCKET_COLORS[b] }}
              title={`${BUCKET_LABELS[b]}: ${c} (${pctText(percentages[b])})`}
              data-testid={`lifecycle-funnel-segment-${b}`}
            >
              {w > 6 ? `${pctText(percentages[b])}` : ""}
            </div>
          );
        })}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] font-mono">
        {order.map((b) => (
          <div key={b} className="flex items-center gap-1">
            <span
              className="inline-block w-2 h-2"
              style={{ backgroundColor: BUCKET_COLORS[b] }}
            />
            <span className="text-slate-300">{BUCKET_LABELS[b]}</span>
            <span className="text-slate-500">
              {counts[b] || 0} · {pctText(percentages[b])}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}


function PerLaneTable({ byLane, order }) {
  const lanes = Object.entries(byLane).filter(
    ([, b]) => Object.values(b).reduce((a, v) => a + v, 0) > 0,
  );
  if (lanes.length === 0) return null;
  return (
    <table
      className="w-full text-[10px] font-mono text-slate-300"
      data-testid="lifecycle-funnel-per-lane"
    >
      <thead>
        <tr className="text-slate-500 uppercase tracking-wider">
          <th className="text-left py-1">Lane</th>
          {order.map((b) => (
            <th key={b} className="text-right pr-1.5"
                style={{ color: BUCKET_COLORS[b] }}>
              {BUCKET_LABELS[b]}
            </th>
          ))}
          <th className="text-right">Total</th>
        </tr>
      </thead>
      <tbody>
        {lanes.map(([lane, buckets]) => {
          const total = Object.values(buckets).reduce((a, v) => a + v, 0);
          return (
            <tr key={lane} className="border-t border-slate-800"
                data-testid={`lifecycle-per-lane-row-${lane}`}>
              <td className="py-1 text-slate-100 uppercase">{lane}</td>
              {order.map((b) => (
                <td key={b} className="text-right pr-1.5">
                  <span style={{ color: BUCKET_COLORS[b] }}>{buckets[b] || 0}</span>
                </td>
              ))}
              <td className="text-right text-slate-200 font-bold">{total}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}


function PerBrainTable({ byBrain, order }) {
  // canonical brains only — sorted descending by total volume.
  const brains = Object.entries(byBrain || {})
    .map(([brain, buckets]) => [
      brain, buckets,
      Object.values(buckets).reduce((a, v) => a + v, 0),
    ])
    .filter(([, , total]) => total > 0)
    .sort((a, b) => b[2] - a[2]);
  if (brains.length === 0) return null;
  return (
    <table
      className="w-full text-[10px] font-mono text-slate-300"
      data-testid="lifecycle-funnel-per-brain"
    >
      <thead>
        <tr className="text-slate-500 uppercase tracking-wider">
          <th className="text-left py-1">Brain</th>
          {order.map((b) => (
            <th key={b} className="text-right pr-1.5"
                style={{ color: BUCKET_COLORS[b] }}>
              {BUCKET_LABELS[b]}
            </th>
          ))}
          <th className="text-right">Total</th>
        </tr>
      </thead>
      <tbody>
        {brains.map(([brain, buckets, total]) => (
          <tr key={brain} className="border-t border-slate-800"
              data-testid={`lifecycle-per-brain-row-${brain}`}>
            <td className="py-1 text-slate-100 capitalize">{brain}</td>
            {order.map((b) => (
              <td key={b} className="text-right pr-1.5">
                <span style={{ color: BUCKET_COLORS[b] }}>{buckets[b] || 0}</span>
              </td>
            ))}
            <td className="text-right text-slate-200 font-bold">{total}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}


function UnknownSamplesList({ samples }) {
  if (!samples || samples.length === 0) return null;
  return (
    <div className="space-y-1" data-testid="lifecycle-funnel-unknown-samples">
      <div className="text-[9px] font-mono uppercase tracking-wider text-red-400">
        Unknown bucket — visibility gaps (last {samples.length})
      </div>
      <div className="text-[10px] font-mono space-y-0.5">
        {samples.map((s) => (
          <div
            key={s.intent_id}
            className="flex justify-between gap-2 text-slate-300"
            data-testid={`lifecycle-unknown-sample-${s.intent_id?.slice(0, 8)}`}
          >
            <span className="truncate">
              <span className="text-slate-500">{s.symbol || "—"}</span>
              <span className="mx-1 text-slate-600">·</span>
              <span className="uppercase">{s.action || "—"}</span>
              <span className="mx-1 text-slate-600">·</span>
              <span className="text-slate-500 lowercase">{s.lane || "—"}</span>
            </span>
            <span className="text-slate-600 text-[9px]">
              {s.has_broker_orders_row ? "ord✓" : "ord✗"}
              {" "}
              {s.has_execution_receipt ? "rcpt✓" : "rcpt✗"}
              {s.broker_status ? ` · ${s.broker_status}` : ""}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}


export default function ExecutionLifecycleFunnelTile() {
  const [data, setData] = useState(null);
  const [hours, setHours] = useState(24);
  const [lane, setLane] = useState("equity");  // equity-focused default
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const params = lane
        ? `?hours=${hours}&lane=${lane}`
        : `?hours=${hours}`;
      const r = await api.get(`/admin/execution-lifecycle/funnel${params}`);
      setData(r.data);
    } catch (e) {
      setErr(e?.message || "fetch failed");
    } finally {
      setLoading(false);
    }
  }, [hours, lane]);

  useEffect(() => {
    fetchData();
    const id = setInterval(fetchData, POLL_MS);
    return () => clearInterval(id);
  }, [fetchData]);

  const order = data?.bucket_order || [
    "filled", "partially_filled", "working", "canceled", "unknown",
  ];

  return (
    <section
      className="border border-slate-800 bg-slate-950/60 p-4 space-y-3"
      data-testid="execution-lifecycle-funnel-tile"
    >
      <header className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-mono uppercase tracking-widest text-slate-200">
            Execution Lifecycle Funnel
          </h3>
          <p className="text-[10px] font-mono text-slate-500 mt-0.5 max-w-md">
            After MC submits to the broker — did the order fill, sit
            working, get canceled, or fall into a visibility gap?
          </p>
        </div>
        <div className="flex items-center gap-1.5">
          {LANE_FILTERS.map((l) => (
            <button
              key={l.value || "all"}
              onClick={() => setLane(l.value)}
              data-testid={`lifecycle-lane-${l.value || "all"}`}
              className={`text-[10px] font-mono uppercase tracking-wider px-1.5 py-0.5 border ${
                lane === l.value
                  ? "border-emerald-500 text-emerald-300 bg-emerald-950/50"
                  : "border-slate-700 text-slate-400 hover:border-slate-500"
              }`}
            >
              {l.label}
            </button>
          ))}
          <span className="mx-1 text-slate-700">·</span>
          {WINDOWS.map((w) => (
            <button
              key={w.value}
              onClick={() => setHours(w.value)}
              data-testid={`lifecycle-window-${w.value}h`}
              className={`text-[10px] font-mono uppercase tracking-wider px-1.5 py-0.5 border ${
                hours === w.value
                  ? "border-emerald-500 text-emerald-300 bg-emerald-950/50"
                  : "border-slate-700 text-slate-400 hover:border-slate-500"
              }`}
            >
              {w.label}
            </button>
          ))}
          <button
            onClick={fetchData}
            disabled={loading}
            data-testid="lifecycle-refresh"
            className="text-slate-500 hover:text-slate-200 disabled:opacity-50"
          >
            <ArrowsClockwise size={14} className={loading ? "animate-spin" : ""} />
          </button>
        </div>
      </header>

      {err && (
        <div
          className="text-[10px] font-mono text-red-400 border border-red-900 bg-red-950/30 px-2 py-1"
          data-testid="lifecycle-error"
        >
          {err}
        </div>
      )}

      {data && (
        <>
          <div className="flex items-baseline justify-between"
               data-testid="lifecycle-total">
            <span className="text-[10px] font-mono text-slate-500 uppercase tracking-wider">
              Executed in {hours}h{lane ? ` · ${lane}` : ""}
            </span>
            <span className="text-lg font-mono font-bold text-slate-100">
              {data.total_executed}
            </span>
          </div>

          <BucketBar
            counts={data.bucket_counts}
            percentages={data.bucket_percentages}
            order={order}
            total={data.total_executed}
          />

          {(!lane || lane === "") && (
            <PerLaneTable byLane={data.by_lane} order={order} />
          )}

          <PerBrainTable byBrain={data.by_brain} order={order} />

          <UnknownSamplesList samples={data.unknown_samples} />

          <div className="flex items-start gap-1 text-[9px] font-mono text-slate-500 pt-1 border-t border-slate-900">
            <Info size={10} className="mt-0.5 shrink-0" />
            <span>
              Source: shared_intents{`{executed:true}`} ⨝ broker_orders
              by broker_order_id. UNKNOWN = no broker_orders row found
              (poller lagging, or order placed off-pipeline).
            </span>
          </div>
        </>
      )}
    </section>
  );
}
