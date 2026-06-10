import React, { useEffect, useState, useMemo } from "react";
import { api, fmtTime, relTime, RUNTIME_META } from "@/lib/api";
import { Card, Badge, EmptyState, LoadingRow } from "@/components/ui-bits";
import { useMcStream } from "@/hooks/useMcStream";

/**
 * Doctrine pin (2026-06-10, P2):
 *
 * The position-misread classifier (shared/position_model.py) writes
 * to `shared_position_misreads` whenever a brain's emit disagrees
 * with broker truth. Before this card, the operator had to query
 * Mongo directly. Now the dashboard shows the last 20 with the
 * 24h verdict gauge, AND new misreads land in real time via SSE —
 * no refresh needed.
 *
 * The card is intentionally LOUD when misreads are accumulating
 * because the 2026-06-09 AAPL incident's prototype row is exactly
 * this shape: brain thinks FLAT, broker says SHORT, BUY emitted —
 * and we need to SEE the next one before it spirals.
 */

const SIDE_COLOR = {
  long:  "#10B981",
  short: "#EF4444",
  flat:  "#71717A",
};

function SidePill({ side }) {
  const color = SIDE_COLOR[side] || "#52525B";
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium uppercase"
      style={{
        background: `${color}22`,
        color,
        border: `1px solid ${color}66`,
      }}
    >
      {side || "—"}
    </span>
  );
}

function VerdictBadge({ verdict }) {
  const palette = {
    no_misreads_in_24h:  { color: "#10B981", label: "CLEAN" },
    isolated:            { color: "#A1A1AA", label: "ISOLATED" },
    elevated:            { color: "#F59E0B", label: "ELEVATED" },
    systemic:            { color: "#EF4444", label: "SYSTEMIC" },
  };
  const meta = palette[verdict] || { color: "#71717A", label: verdict || "—" };
  return <Badge color={meta.color} testid="misread-24h-verdict">{meta.label}</Badge>;
}

export default function PositionMisreadsCard() {
  const [rows, setRows] = useState(null);
  const [summary, setSummary] = useState(null);
  const [err, setErr] = useState(null);
  const { byType, connected } = useMcStream({ cap: 30 });

  // Initial seed from REST.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [recent, summary24h] = await Promise.all([
          api.get("/admin/position-misreads/recent?limit=20"),
          api.get("/admin/position-misreads/summary-24h"),
        ]);
        if (cancelled) return;
        // Endpoint returns `{items: [...], count: N}` — use items.
        const items = Array.isArray(recent.data?.items)
          ? recent.data.items
          : Array.isArray(recent.data)
            ? recent.data
            : [];
        setRows(items);
        setSummary(summary24h.data);
      } catch (e) {
        if (cancelled) return;
        setErr(e?.response?.data?.detail || e.message);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Merge live SSE misreads on top of the seed list.
  const merged = useMemo(() => {
    if (rows == null) return null;
    const safeRows = Array.isArray(rows) ? rows : [];
    const liveOnes = byType.position_misread || [];
    if (liveOnes.length === 0) return safeRows;
    // Dedup by detected_at + symbol + brain.
    const seen = new Set();
    const all = [...liveOnes, ...safeRows];
    const out = [];
    for (const r of all) {
      const key = `${r.detected_at}|${r.symbol}|${r.brain}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(r);
      if (out.length >= 20) break;
    }
    return out;
  }, [rows, byType.position_misread]);

  return (
    <Card testid="position-misreads-card">
      <div className="flex items-start justify-between mb-3">
        <div>
          <div className="text-xs uppercase tracking-wider text-zinc-500">
            Position Misreads · last 20
          </div>
          <div className="text-sm text-zinc-400 mt-1">
            Recorded when a brain&apos;s claimed position evolution disagrees with broker truth.
            The 2026-06-09 AAPL pattern lives here.
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {summary && <VerdictBadge verdict={summary?.verdict} />}
          <span
            className="h-2 w-2 rounded-full"
            style={{ background: connected ? "#10B981" : "#71717A" }}
            data-testid="misreads-live-dot"
          />
        </div>
      </div>

      {summary && (
        <div className="grid grid-cols-3 gap-3 mb-4 text-sm">
          <div className="bg-zinc-900/50 rounded px-3 py-2">
            <div className="text-xs text-zinc-500 uppercase">Misreads (24h)</div>
            <div className="text-xl font-mono mt-1" data-testid="misread-24h-count">
              {summary.total ?? 0}
            </div>
          </div>
          <div className="bg-zinc-900/50 rounded px-3 py-2">
            <div className="text-xs text-zinc-500 uppercase">Missed Short Profit</div>
            <div
              className="text-xl font-mono mt-1"
              style={{ color: (summary.missed_short_profit ?? 0) > 0 ? "#EF4444" : "#A1A1AA" }}
              data-testid="misread-missed-short-count"
            >
              {summary.missed_short_profit ?? 0}
            </div>
          </div>
          <div className="bg-zinc-900/50 rounded px-3 py-2">
            <div className="text-xs text-zinc-500 uppercase">Verdict</div>
            <div className="text-sm font-medium mt-1 text-zinc-300">
              {summary.verdict || "—"}
            </div>
          </div>
        </div>
      )}

      {err && (
        <div className="text-sm text-rose-400 mb-3" data-testid="misreads-error">
          {err}
        </div>
      )}

      {merged == null ? (
        <LoadingRow />
      ) : merged.length === 0 ? (
        <EmptyState message="No position misreads recorded. The brains agree with broker truth." testid="misreads-empty" />
      ) : (
        <div className="overflow-x-auto" data-testid="misreads-table">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs uppercase tracking-wider text-zinc-500 border-b border-zinc-800">
                <th className="text-left py-2 font-medium">Detected</th>
                <th className="text-left py-2 font-medium">Brain</th>
                <th className="text-left py-2 font-medium">Symbol</th>
                <th className="text-left py-2 font-medium">Action</th>
                <th className="text-left py-2 font-medium">Brain thought</th>
                <th className="text-left py-2 font-medium">Broker said</th>
                <th className="text-right py-2 font-medium">Qty</th>
                <th className="text-left py-2 font-medium">Flags</th>
              </tr>
            </thead>
            <tbody>
              {merged.map((r, i) => (
                <tr
                  key={`${r.detected_at}-${i}`}
                  className="border-b border-zinc-900 hover:bg-zinc-900/40"
                  data-testid={`misread-row-${i}`}
                >
                  <td className="py-2 text-zinc-400 text-xs whitespace-nowrap">
                    <div title={r.detected_at}>{relTime(r.detected_at)}</div>
                  </td>
                  <td className="py-2">
                    {(() => {
                      const m = RUNTIME_META[(r.brain || "").toLowerCase()];
                      const name = m?.roleTitle || r.brain || "—";
                      return (
                        <span style={{ color: m?.color || undefined }}>{name}</span>
                      );
                    })()}
                  </td>
                  <td className="py-2 font-mono">{r.symbol}</td>
                  <td className="py-2">
                    <Badge color="#7B5CFF">{r.emitted_action}</Badge>
                  </td>
                  <td className="py-2"><SidePill side={r.assumed_side} /></td>
                  <td className="py-2"><SidePill side={r.actual_side} /></td>
                  <td className="py-2 font-mono text-right text-xs">
                    {r.actual_signed_qty?.toFixed?.(4) ?? r.actual_signed_qty}
                  </td>
                  <td className="py-2">
                    {r.missed_short_profit && (
                      <Badge color="#EF4444">MISSED_SHORT</Badge>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
