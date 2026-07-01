import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { Card, Badge, EmptyState } from "@/components/ui-bits";
import { ArrowsClockwise, MagnifyingGlass, Warning, CheckCircle, XCircle, Pulse } from "@phosphor-icons/react";

const WINDOWS = [1, 6, 24, 72];

const BRAIN_COLOR = {
  camino: "#3B82F6",
  barracuda: "#F59E0B",
  hellcat: "#10B981",
  gto: "#DC2626",
};

const LANE_COLOR = { equity: "#F59E0B", crypto: "#8B5CF6" };

function tsNow() { return Date.now(); }

function withinWindow(iso, hours) {
  if (!iso) return false;
  const t = new Date(iso).getTime();
  if (isNaN(t)) return false;
  return t >= tsNow() - hours * 3600 * 1000;
}

/**
 * TraderPostMortem — answers ONE operator question:
 *   "Why isn't the trader firing?"
 *
 * Successor to the old 1500-line `IntentPostMortemPanel` which was
 * pinned to the deleted 16-gate pipeline. The new architecture has
 * exactly 5 possible outcomes per cycle (fire / hold / risk-block /
 * broker-error / fetch-fail) so the panel is now honest and small.
 *
 * All data comes from `/api/admin/trader/receipts` — local SQLite,
 * Mongo-independent. Aggregation happens client-side because the
 * volume is bounded (one row per lane per minute → ~2880/day max).
 */
export default function TraderPostMortem() {
  const [hours, setHours] = useState(24);
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [traceId, setTraceId] = useState("");
  const [trace, setTrace] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.get("/admin/trader/receipts", { params: { limit: 500 } });
      setRows(res.data?.items || []);
      setErr("");
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  const inWindow = useMemo(
    () => rows.filter((r) => withinWindow(r.ts, hours)),
    [rows, hours],
  );

  const summary = useMemo(() => aggregate(inWindow), [inWindow]);

  const runTrace = () => {
    const id = traceId.trim();
    if (!id) return setTrace(null);
    const hit = rows.find(
      (r) => r.cycle_id === id || (r.cycle_id || "").startsWith(id),
    );
    setTrace(hit || { _not_found: true, id });
  };

  return (
    <Card className="mb-6" testid="trader-post-mortem">
      {/* Header */}
      <div className="flex items-start justify-between gap-3 mb-4">
        <div className="flex items-start gap-3">
          <div className="mt-0.5 text-rd-dim">
            <Pulse size={16} weight="duotone" />
          </div>
          <div>
            <div className="font-display text-base font-bold text-rd-text leading-none">
              Trader Post-Mortem
            </div>
            <div className="text-[11px] text-rd-muted mt-1 font-mono leading-relaxed">
              Why isn&apos;t the trader firing? Aggregates the last N hours of receipts from local SQLite.
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1">
          {WINDOWS.map((h) => (
            <button
              key={`window-${h}`}
              onClick={() => setHours(h)}
              className={
                "px-2 py-1 text-[10px] font-mono uppercase tracking-wider border " +
                (hours === h
                  ? "border-rd-text text-rd-text"
                  : "border-rd-border text-rd-dim hover:text-rd-text")
              }
              data-testid={`post-mortem-window-${h}h`}
            >
              {h}h
            </button>
          ))}
          <button
            onClick={load}
            disabled={loading}
            className="ml-1 p-1.5 border border-rd-border text-rd-dim hover:text-rd-text"
            data-testid="post-mortem-reload"
          >
            <ArrowsClockwise size={12} weight="bold" className={loading ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {err && (
        <div className="border border-rd-danger text-rd-danger px-3 py-2 mb-3 text-xs font-mono" data-testid="post-mortem-error">
          {err}
        </div>
      )}

      {/* Headline strip */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-2 mb-4" data-testid="post-mortem-headline">
        <Stat label="Cycles" value={summary.total} testid="post-mortem-total" />
        <Stat label="Fired" value={summary.fired} color="#10B981" testid="post-mortem-fired" />
        <Stat label="Hold" value={summary.hold} color="#64748B" testid="post-mortem-hold" />
        <Stat label="Risk block" value={summary.risk_blocked} color="#F59E0B" testid="post-mortem-risk-blocked" />
        <Stat label="Errors" value={summary.errors} color="#DC2626" testid="post-mortem-errors" />
      </div>

      {inWindow.length === 0 ? (
        <EmptyState
          message="No receipts in this window. Either the trader isn't running or you just deployed. Shift TRADER_ENABLED=true and wait one interval."
          testid="post-mortem-empty"
        />
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 mb-4">
          <ReasonHistogram
            title="Top HOLD reasons"
            testid="post-mortem-hold-histogram"
            items={summary.hold_reasons}
            accent="#64748B"
          />
          <ReasonHistogram
            title="Top RISK block reasons"
            testid="post-mortem-risk-histogram"
            items={summary.risk_reasons}
            accent="#F59E0B"
          />
        </div>
      )}

      {/* Per-brain × verdict */}
      {inWindow.length > 0 && (
        <div className="mb-4">
          <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2">
            Advisor signals (per brain × verdict)
          </div>
          <div className="border border-rd-border">
            <div className="grid grid-cols-5 gap-2 px-2 py-1.5 bg-rd-bg text-[9px] uppercase tracking-widest text-rd-dim font-mono">
              <div>brain</div>
              <div className="text-center">buy</div>
              <div className="text-center">sell</div>
              <div className="text-center">hold</div>
              <div className="text-center">total</div>
            </div>
            {Object.entries(summary.by_brain).map(([brain, counts]) => (
              <div
                key={`by-brain-${brain}`}
                className="grid grid-cols-5 gap-2 px-2 py-1.5 items-center text-[11px] font-mono border-t border-rd-border/40"
                data-testid={`post-mortem-brain-row-${brain}`}
              >
                <div
                  className="uppercase font-bold tracking-wide"
                  style={{ color: BRAIN_COLOR[brain] || "#A1A1AA" }}
                >
                  {brain}
                </div>
                <div className="text-center text-rd-success">{counts.BUY || 0}</div>
                <div className="text-center text-rd-danger">{counts.SELL || 0}</div>
                <div className="text-center text-rd-dim">{counts.HOLD || 0}</div>
                <div className="text-center text-rd-text">{counts._total || 0}</div>
              </div>
            ))}
            {Object.keys(summary.by_brain).length === 0 && (
              <div className="px-2 py-2 text-[11px] text-rd-dim font-mono">
                no advisor signals in window
              </div>
            )}
          </div>
        </div>
      )}

      {/* Per-lane summary */}
      {inWindow.length > 0 && (
        <div className="mb-4 grid grid-cols-1 md:grid-cols-2 gap-2" data-testid="post-mortem-by-lane">
          {["equity", "crypto"].map((lane) => {
            const s = summary.by_lane[lane] || { total: 0, fired: 0, hold: 0, risk_blocked: 0, errors: 0 };
            return (
              <div
                key={`lane-${lane}`}
                className="border border-rd-border px-2 py-1.5"
                data-testid={`post-mortem-lane-${lane}`}
              >
                <div
                  className="text-[10px] uppercase tracking-widest mb-1 font-mono"
                  style={{ color: LANE_COLOR[lane] }}
                >
                  {lane}
                </div>
                <div className="text-[11px] font-mono text-rd-muted flex items-center gap-2 flex-wrap">
                  <span>{s.total} cycles</span>
                  <span className="text-rd-success">· {s.fired} fired</span>
                  <span className="text-rd-dim">· {s.hold} hold</span>
                  <span className="text-rd-warn">· {s.risk_blocked} risk</span>
                  <span className="text-rd-danger">· {s.errors} err</span>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Per-cycle trace */}
      <div className="border-t border-rd-border pt-3">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim mb-2">
          Trace one cycle
        </div>
        <div className="flex items-center gap-2 mb-2">
          <input
            type="text"
            value={traceId}
            onChange={(e) => setTraceId(e.target.value)}
            placeholder="cycle_id (or prefix)"
            className="flex-1 bg-rd-bg border border-rd-border px-2 py-1.5 font-mono text-[11px] text-rd-text focus:border-rd-accent focus:outline-none"
            data-testid="post-mortem-trace-input"
          />
          <button
            onClick={runTrace}
            disabled={!traceId.trim()}
            className="px-3 py-1.5 text-[11px] font-mono uppercase tracking-wider border border-rd-border text-rd-dim hover:text-rd-text disabled:opacity-40 disabled:cursor-not-allowed"
            data-testid="post-mortem-trace-run"
          >
            <MagnifyingGlass size={11} weight="bold" className="inline mr-1" />
            trace
          </button>
        </div>
        {trace?._not_found && (
          <div className="text-[11px] font-mono text-rd-warn" data-testid="post-mortem-trace-not-found">
            No receipt with cycle_id {trace.id} in loaded window. Try a wider window or paste the full id.
          </div>
        )}
        {trace && !trace._not_found && (
          <div className="border border-rd-border bg-rd-bg p-2 font-mono text-[10px] text-rd-text overflow-x-auto" data-testid="post-mortem-trace-detail">
            <pre className="whitespace-pre-wrap break-all">{JSON.stringify(trace, null, 2)}</pre>
          </div>
        )}
      </div>
    </Card>
  );
}

function Stat({ label, value, color, testid }) {
  return (
    <div className="border border-rd-border bg-rd-bg px-2 py-1.5" data-testid={testid}>
      <div className="text-[9px] uppercase tracking-widest text-rd-dim">{label}</div>
      <div
        className="font-mono text-sm mt-0.5"
        style={{ color: color || "#F5F5F5" }}
      >
        {value}
      </div>
    </div>
  );
}

function ReasonHistogram({ title, items, accent, testid }) {
  const top = items.slice(0, 6);
  const max = top[0]?.count || 1;
  return (
    <div className="border border-rd-border" data-testid={testid}>
      <div className="px-2 py-1.5 border-b border-rd-border bg-rd-bg">
        <div className="text-[10px] uppercase tracking-widest text-rd-dim font-mono">
          {title}
        </div>
      </div>
      {top.length === 0 ? (
        <div className="px-2 py-2 text-[11px] font-mono text-rd-dim">
          none in window
        </div>
      ) : (
        <div className="divide-y divide-rd-border/40">
          {top.map((item) => (
            <div
              key={`reason-${item.reason}`}
              className="px-2 py-1.5"
              data-testid={`${testid}-row-${(item.reason || "r").replace(/[^a-z0-9]/gi, "").slice(0, 24)}`}
            >
              <div className="flex items-baseline justify-between gap-2 mb-1">
                <span className="font-mono text-[11px] text-rd-text truncate" title={item.reason}>
                  {item.reason || "—"}
                </span>
                <span className="font-mono text-[11px] shrink-0" style={{ color: accent }}>
                  ×{item.count}
                </span>
              </div>
              <div className="h-1 bg-rd-border/40">
                <div
                  className="h-1"
                  style={{
                    width: `${Math.round((item.count / max) * 100)}%`,
                    backgroundColor: accent,
                  }}
                />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Client-side aggregation. Given N receipts, bucket them into:
 *   fired / hold / risk_blocked / errors
 * and rank the top reasons for each blocking category.
 */
function aggregate(rows) {
  const out = {
    total: rows.length,
    fired: 0, hold: 0, risk_blocked: 0, errors: 0,
    hold_reasons: {},
    risk_reasons: {},
    by_brain: {},
    by_lane: {
      equity: { total: 0, fired: 0, hold: 0, risk_blocked: 0, errors: 0 },
      crypto: { total: 0, fired: 0, hold: 0, risk_blocked: 0, errors: 0 },
    },
  };

  for (const r of rows) {
    const chosen = r.chosen || {};
    const risk = r.risk || {};
    const lane = r.lane || "equity";
    const laneBucket = out.by_lane[lane] || (out.by_lane[lane] = {
      total: 0, fired: 0, hold: 0, risk_blocked: 0, errors: 0,
    });
    laneBucket.total += 1;

    // advisor signals (all 4 brains that opined)
    for (const s of r.signals || []) {
      const b = s.brain;
      if (!b) continue;
      const bucket = out.by_brain[b] || (out.by_brain[b] = { BUY: 0, SELL: 0, HOLD: 0, _total: 0 });
      const v = String(s.verdict || "").toUpperCase();
      if (bucket[v] !== undefined) bucket[v] += 1;
      bucket._total += 1;
    }

    if (r.error) {
      out.errors += 1;
      laneBucket.errors += 1;
      continue;
    }
    if (r.broker_result && !r.error) {
      out.fired += 1;
      laneBucket.fired += 1;
      continue;
    }
    // No broker call. Either risk blocked or executor said HOLD /
    // no signal / below threshold.
    if (risk.ok === false && risk.reason) {
      out.risk_blocked += 1;
      laneBucket.risk_blocked += 1;
      out.risk_reasons[risk.reason] = (out.risk_reasons[risk.reason] || 0) + 1;
      continue;
    }
    // executor HOLD, below-threshold, or seat-vacant → treated as HOLD.
    out.hold += 1;
    laneBucket.hold += 1;
    const holdReason =
      chosen.verdict === "HOLD" ? "executor_verdict_hold" :
      (risk.reason || "no_executor_signal");
    out.hold_reasons[holdReason] = (out.hold_reasons[holdReason] || 0) + 1;
  }

  return {
    ...out,
    hold_reasons: sortHistogram(out.hold_reasons),
    risk_reasons: sortHistogram(out.risk_reasons),
  };
}

function sortHistogram(bucket) {
  return Object.entries(bucket)
    .map(([reason, count]) => ({ reason, count }))
    .sort((a, b) => b.count - a.count);
}
