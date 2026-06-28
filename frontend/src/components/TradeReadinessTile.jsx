/**
 * TradeReadinessTile — single-glance "why isn't equity trading?"
 * (2026-02-25)
 *
 * Operator pin:
 *   "The backend already exposed the truth (brain_hold = 99.5%).
 *    Now make that visible without curl. Show 24h total intents,
 *    top failing gate, colored histogram bars, newest blocked
 *    intents, click gate → filtered drilldown."
 *
 * Backs onto GET /api/admin/equity-trade-readiness which returns
 * the persisted-field truth (no doctrine recompute):
 *   - fleet_summary.by_first_failing_gate (the histogram)
 *   - fleet_summary.total_intents_window (the headline)
 *   - items[] (newest first, includes blockers + first_failing_gate)
 *   - gate_order (canonical chain)
 *
 * Click on a gate bar → re-query the endpoint without a server
 * filter (the gate filter happens client-side over `items`) and
 * highlight rows whose first_failing_gate matches.
 *
 * READ-ONLY. No mutations. Polls every 30s like IntentFunnelTile.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowsClockwise,
  Compass,
  XCircle,
  Funnel,
  Tag,
} from "@phosphor-icons/react";
import { api } from "@/lib/api";

const POLL_MS = 30_000;
const WINDOWS = [1, 6, 24, 72];

// Color per gate — high-contrast so the histogram tells the
// story on a phone screen without squinting. Keeps the rd-*
// semantic palette consistent with the rest of the Intents page.
const GATE_COLORS = {
  brain_hold:     "bg-rd-warn",      // amber — most common; the brain's own floor
  seat_holder:    "bg-rd-accent",    // accent — operator config
  market_hours:   "bg-blue-500",     // blue — environmental
  dry_run:        "bg-rd-danger",    // red — real reject
  consensus:      "bg-purple-500",   // purple — peer-veto-by-math
  action_allowed: "bg-orange-500",   // orange — SHORT/COVER mismatch
  rr_validity:    "bg-pink-500",     // pink — bad target/stop
  roadguard:      "bg-rd-danger",    // red — manipulation flag
  all_pass:       "bg-emerald-500",  // green — actually passed
};


function StatCell({ label, value, testid, valueClassName }) {
  return (
    <div
      className="border border-rd-border bg-rd-bg p-1.5 text-center"
      data-testid={testid}
    >
      <div className="font-mono text-[9px] uppercase text-rd-dim">{label}</div>
      <div className={`font-mono text-lg ${valueClassName || "text-rd-text"}`}>{value}</div>
    </div>
  );
}


function GateBar({ name, count, total, color, isTop, isSelected, onClick }) {
  const pct = total > 0 ? (100 * count) / total : 0;
  return (
    <button
      onClick={onClick}
      className={
        "w-full text-left border bg-rd-bg p-1.5 space-y-1 hover:border-rd-accent transition-colors " +
        (isSelected ? "border-rd-accent border-2 " : "border-rd-border ") +
        (isTop && !isSelected ? "ring-1 ring-rd-warn/50" : "")
      }
      data-testid={`readiness-gate-${name}`}
      title={`Click to filter newest intents by first_failing_gate=${name}`}
    >
      <div className="flex items-baseline justify-between font-mono text-[10px]">
        <span className="text-rd-text uppercase tracking-wider">
          {name.replace(/_/g, " ")}
          {isTop && (
            <span
              className="ml-1.5 text-rd-warn font-bold"
              data-testid={`readiness-top-gate-marker-${name}`}
            >
              ← top
            </span>
          )}
        </span>
        <span className="text-rd-dim">
          <span className="text-rd-text font-bold">{count}</span>
          {" · "}
          {pct.toFixed(1)}%
        </span>
      </div>
      <div className="h-1.5 bg-rd-bg2 relative overflow-hidden">
        <div
          className={`h-full ${color}`}
          style={{ width: `${Math.max(2, Math.min(100, pct))}%` }}
        />
      </div>
    </button>
  );
}


function IntentRow({ item, isHighlighted }) {
  const ts = item.ingest_ts ? new Date(item.ingest_ts).toISOString().slice(11, 19) : "—";
  const ffg = item.first_failing_gate || "all_pass";
  const ffgColor = GATE_COLORS[ffg] || "bg-rd-bg2";
  return (
    <div
      className={
        "grid grid-cols-12 gap-2 items-center border-b border-rd-border/30 px-1.5 py-1 font-mono text-[10px] " +
        (isHighlighted ? "bg-rd-warn/10" : "")
      }
      data-testid={`readiness-intent-row-${item.intent_id}`}
    >
      <div className="col-span-2 text-rd-dim">{ts}</div>
      <div className="col-span-2 text-rd-text uppercase">{item.stack}</div>
      <div className="col-span-2 text-rd-text">{item.symbol}</div>
      <div className="col-span-3 text-rd-dim">
        <span className="text-rd-text">{item.raw_action}</span>
        {" → "}
        <span className={item.broker_action ? "text-rd-text" : "text-rd-danger"}>
          {item.broker_action || "null"}
        </span>
      </div>
      <div className="col-span-3 flex items-center gap-1.5">
        <span className={`inline-block h-2 w-2 ${ffgColor}`} />
        <span className="text-rd-text uppercase tracking-wider">
          {ffg.replace(/_/g, " ")}
        </span>
      </div>
    </div>
  );
}


export default function TradeReadinessTile() {
  const [hours, setHours] = useState(24);
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);
  const [selectedGate, setSelectedGate] = useState(null);
  const lastTopGateRef = useRef(null);

  const load = useCallback(async (h) => {
    setLoading(true);
    try {
      // limit=50 — newest blocked intents fill the bottom table.
      // hours=h drives both the items + the fleet histogram.
      const res = await api.get(
        `/admin/equity-trade-readiness?limit=50&hours=${h}`,
      );
      setData(res.data);
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

  // Sort the histogram by count desc, derive the top gate.
  const sortedGates = useMemo(() => {
    if (!data?.fleet_summary?.by_first_failing_gate) return [];
    return Object.entries(data.fleet_summary.by_first_failing_gate)
      .sort((a, b) => b[1] - a[1])
      .map(([name, count]) => ({ name, count }));
  }, [data]);

  const topGate = sortedGates[0]?.name || null;
  const total = data?.fleet_summary?.total_intents_window || 0;

  // Remember the top gate for the "shift" indicator. If it
  // changes between polls, the operator likely just unblocked a
  // gate via the tuning UI — surface that as a small note.
  useEffect(() => {
    if (topGate && lastTopGateRef.current && lastTopGateRef.current !== topGate) {
      // Don't toast — the tile shows the new top inline.
      // Just update the ref so we know which gate to highlight.
    }
    if (topGate) lastTopGateRef.current = topGate;
  }, [topGate]);

  // Client-side filter of items by selected gate. We do this
  // client-side rather than re-querying so clicks feel instant
  // and the histogram never shifts under the operator's finger.
  const visibleItems = useMemo(() => {
    if (!data?.items) return [];
    if (!selectedGate) return data.items;
    return data.items.filter((it) => (it.first_failing_gate || "all_pass") === selectedGate);
  }, [data, selectedGate]);

  return (
    <div
      className="border-2 border-rd-accent/60 bg-rd-bg2 p-2.5 space-y-2"
      data-testid="trade-readiness-tile"
    >
      <div className="flex items-center gap-2">
        <Compass size={13} weight="bold" className="text-rd-accent" />
        <div className="flex-1">
          <div
            className="font-mono text-[11px] uppercase tracking-widest text-rd-text font-bold"
            data-testid="trade-readiness-title"
          >
            Trade Readiness · Why isn&apos;t equity trading?
          </div>
          <div className="font-mono text-[9px] text-rd-dim mt-0.5">
            Persisted-truth view — no doctrine recompute. Click a gate to filter the rows.
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
              data-testid={`readiness-window-${h}h`}
            >
              {h}h
            </button>
          ))}
          <button
            onClick={() => load(hours)}
            disabled={loading}
            className="p-1 border border-rd-border text-rd-dim hover:text-rd-text"
            data-testid="readiness-reload"
            title="Reload now"
          >
            <ArrowsClockwise size={11} weight="bold" className={loading ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {err && (
        <div
          className="border border-rd-danger bg-rd-bg p-2 font-mono text-[10px] text-rd-danger flex items-start gap-1.5"
          data-testid="readiness-error"
        >
          <XCircle size={11} weight="bold" className="mt-0.5 shrink-0" />
          {err}
        </div>
      )}

      {data && (
        <>
          {/* ── Headline stats ──────────────────────────────── */}
          <div className="grid grid-cols-3 gap-2" data-testid="readiness-headline">
            <StatCell
              label={`Intents (${hours}h)`}
              value={total}
              testid="readiness-stat-total"
            />
            <StatCell
              label="Equity Seat"
              value={data.seat?.equity_executor || "—"}
              testid="readiness-stat-seat"
            />
            <StatCell
              label="Equity Lane"
              value={data.session?.lane_status || "—"}
              valueClassName={
                data.session?.lane_status === "OPEN"
                  ? "text-emerald-400"
                  : data.session?.lane_status === "GATED"
                    ? "text-rd-warn"
                    : data.session?.lane_status === "DISABLED"
                      ? "text-rd-danger"
                      : "text-rd-text"
              }
              testid="readiness-stat-market"
            />
          </div>

          {/* ── Top-failing-gate banner ──────────────────────── */}
          {topGate && (
            <div
              className="border-2 border-rd-warn bg-rd-warn/10 p-2 font-mono text-[11px] text-rd-warn flex items-start gap-1.5"
              data-testid="readiness-top-failing-banner"
            >
              <Funnel size={12} weight="bold" className="mt-0.5 shrink-0" />
              <span>
                <span className="font-bold uppercase tracking-wider">Top failing gate:</span>{" "}
                <span className="font-bold uppercase">{topGate.replace(/_/g, " ")}</span>
                {" — "}
                <span className="text-rd-text font-bold">
                  {sortedGates[0]?.count || 0}
                </span>
                {" of "}
                <span className="text-rd-text font-bold">{total}</span>
                {" intents ("}
                {total > 0 ? ((100 * (sortedGates[0]?.count || 0)) / total).toFixed(1) : 0}
                {"%)."}
              </span>
            </div>
          )}

          {/* ── Histogram bars (clickable) ──────────────────── */}
          <div className="space-y-1" data-testid="readiness-histogram">
            {sortedGates.map((g) => (
              <GateBar
                key={g.name}
                name={g.name}
                count={g.count}
                total={total}
                color={GATE_COLORS[g.name] || "bg-rd-bg2"}
                isTop={g.name === topGate}
                isSelected={selectedGate === g.name}
                onClick={() =>
                  setSelectedGate((s) => (s === g.name ? null : g.name))
                }
              />
            ))}
          </div>

          {/* ── Newest blocked intents table ─────────────────── */}
          <div className="border border-rd-border bg-rd-bg" data-testid="readiness-items">
            <div className="flex items-center justify-between border-b border-rd-border px-1.5 py-1">
              <div className="flex items-center gap-1.5">
                <Tag size={11} weight="bold" className="text-rd-dim" />
                <span className="font-mono text-[10px] uppercase tracking-wider text-rd-text">
                  Newest intents
                  {selectedGate && (
                    <span className="text-rd-accent ml-1.5">
                      · filter: {selectedGate.replace(/_/g, " ")}
                    </span>
                  )}
                </span>
              </div>
              {selectedGate && (
                <button
                  onClick={() => setSelectedGate(null)}
                  className="font-mono text-[9px] uppercase text-rd-dim hover:text-rd-text border border-rd-border px-1.5 py-0.5"
                  data-testid="readiness-clear-filter"
                >
                  Clear filter
                </button>
              )}
            </div>
            <div className="grid grid-cols-12 gap-2 border-b border-rd-border/50 px-1.5 py-1 font-mono text-[9px] uppercase text-rd-dim tracking-wider">
              <div className="col-span-2">Time</div>
              <div className="col-span-2">Brain</div>
              <div className="col-span-2">Symbol</div>
              <div className="col-span-3">Action → Broker</div>
              <div className="col-span-3">First failing gate</div>
            </div>
            {visibleItems.length === 0 ? (
              <div
                className="px-1.5 py-3 font-mono text-[10px] text-rd-dim text-center"
                data-testid="readiness-empty"
              >
                {selectedGate
                  ? `no intents in this view failed at ${selectedGate}`
                  : "no equity intents in this window"}
              </div>
            ) : (
              visibleItems.map((item) => (
                <IntentRow
                  key={item.intent_id}
                  item={item}
                  isHighlighted={
                    selectedGate &&
                    (item.first_failing_gate || "all_pass") === selectedGate
                  }
                />
              ))
            )}
          </div>

          {/* Footer — policy hint so the operator sees the allowed_actions list */}
          {data.policy?.allowed_actions && (
            <div
              className="font-mono text-[9px] text-rd-dim border-t border-rd-border pt-1.5"
              data-testid="readiness-policy-footer"
            >
              auto-submit policy: allowed_actions ={" "}
              <span className="text-rd-text">
                [{data.policy.allowed_actions.join(", ")}]
              </span>
              {" · seat from "}
              <span className="text-rd-text">{data.seat?.source}</span>
            </div>
          )}
        </>
      )}
    </div>
  );
}
