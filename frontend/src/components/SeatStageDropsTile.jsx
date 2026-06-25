/**
 * SeatStageDropsTile — answers "structural vs real rejection?" at
 * a glance.
 *
 * Operator pin (2026-02-23): With infrastructure recovered, the
 * dominant funnel leak is at the seat gate (~94% of emitted intents
 * dropping there). The structural baseline is ~75% (1 executor + 3
 * advisors → 3-in-4 emits land on brain_not_current_seat_holder by
 * design). This tile breaks the 94% into canonical reason buckets
 * + per-brain + per-lane + per-seat cross-tabs so the operator can
 * tell whether the remaining ~19% is doctrine_reject, confidence
 * floor, or runtime issues.
 *
 * Read-only. Polls /api/admin/execution-funnel/seat-stage-drops
 * every 15s.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowsClockwise, Warning, Info } from "@phosphor-icons/react";
import { api } from "@/lib/api";

const POLL_MS = 15_000;
const WINDOWS = [
  { label: "1h",  value: 1 },
  { label: "6h",  value: 6 },
  { label: "24h", value: 24 },
  { label: "72h", value: 72 },
];

const COLOR_BY_KEY = {
  neutral: "#6B7280",  // grey
  warn:    "#F59E0B",  // amber — tunable
  error:   "#EF4444",  // red — runtime issue
  info:    "#3B82F6",  // blue — informational
};


function pct(x) {
  if (x == null || Number.isNaN(x)) return "—";
  return `${(x * 100).toFixed(1)}%`;
}


function CategoryBadge({ category, color }) {
  const fg = COLOR_BY_KEY[color] || COLOR_BY_KEY.neutral;
  return (
    <span
      className="font-mono text-[8px] uppercase tracking-widest px-1 py-px border"
      style={{ color: fg, borderColor: fg + "55" }}
      data-testid={`seat-drops-category-${(category || "other").toLowerCase()}`}
    >
      {(category || "other").replace(/_/g, " ")}
    </span>
  );
}


export default function SeatStageDropsTile() {
  const [data, setData] = useState(null);
  const [hours, setHours] = useState(24);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);
  const [lastRefresh, setLastRefresh] = useState(null);
  const [showBrainBreakdown, setShowBrainBreakdown] = useState(false);
  const [showSeatBreakdown, setShowSeatBreakdown] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const out = await api.get(
        `/admin/execution-funnel/seat-stage-drops?hours=${hours}`,
      );
      setData(out.data);
      setErr(null);
      setLastRefresh(new Date());
    } catch (e) {
      setErr(e?.message || "fetch failed");
    } finally {
      setLoading(false);
    }
  }, [hours]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, POLL_MS);
    return () => clearInterval(t);
  }, [refresh]);

  // Dominant category for the headline interpretation.
  const headline = useMemo(() => {
    if (!data?.reasons?.length) return null;
    const top = data.reasons[0];
    if (top.reason === "brain_not_current_seat_holder" && top.pct >= 0.50) {
      return {
        text:  "Dominant drop is STRUCTURAL — leave consensus + conf_min alone.",
        color: COLOR_BY_KEY.neutral,
      };
    }
    if (top.category === "THRESHOLD_TOO_TIGHT") {
      return {
        text:  "Confidence floor is the dominant rejector — consider lowering per-seat conf_min.",
        color: COLOR_BY_KEY.warn,
      };
    }
    if (top.category === "RUNTIME_SEAT_ISSUE") {
      return {
        text:  `Runtime issue dominates: ${top.reason}. Check seat assignment / trust map.`,
        color: COLOR_BY_KEY.error,
      };
    }
    return null;
  }, [data]);

  return (
    <div
      className="border border-rd-border bg-rd-bg p-3 space-y-3"
      data-testid="seat-stage-drops-tile"
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-rd-dim">
            Top Seat-Stage Drops
          </div>
          <div className="font-mono text-[10px] text-rd-dim mt-0.5 flex items-center gap-1">
            <Info size={10} />
            <span>Structural vs real rejection · per-brain · per-seat</span>
          </div>
        </div>
        <div className="flex items-center gap-1">
          {WINDOWS.map((w) => (
            <button
              key={w.value}
              onClick={() => setHours(w.value)}
              className={`font-mono text-[10px] px-1.5 py-0.5 border transition-colors ${
                hours === w.value
                  ? "border-rd-text/60 text-rd-text bg-rd-text/5"
                  : "border-rd-border text-rd-dim hover:border-rd-text/40"
              }`}
              data-testid={`seat-drops-window-${w.value}h`}
            >
              {w.label}
            </button>
          ))}
          <button
            onClick={refresh}
            className="text-rd-dim hover:text-rd-text ml-1"
            data-testid="seat-drops-refresh"
            title="Refresh now"
          >
            <ArrowsClockwise size={14} />
          </button>
        </div>
      </div>

      {err && (
        <div
          className="flex items-center gap-1.5 text-[10px] text-amber-500"
          data-testid="seat-drops-error"
        >
          <Warning size={12} />
          <span>{err}</span>
        </div>
      )}

      {loading && !data && (
        <div className="text-rd-dim text-[10px] py-2">Loading…</div>
      )}

      {!loading && data && data.total_seat_rejected === 0 && (
        <div className="text-rd-dim text-[10px] py-3" data-testid="seat-drops-empty">
          No seat-stage rejections in the last {hours}h.
        </div>
      )}

      {!loading && data && data.total_seat_rejected > 0 && (
        <>
          {/* Summary band */}
          <div className="grid grid-cols-3 gap-2">
            <div data-testid="seat-drops-total">
              <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim">
                Seat-rejected
              </div>
              <div className="font-mono text-base text-rd-text mt-0.5">
                {data.total_seat_rejected.toLocaleString()}
              </div>
            </div>
            <div data-testid="seat-drops-structural">
              <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim">
                Structural
              </div>
              <div
                className="font-mono text-base mt-0.5"
                style={{ color: COLOR_BY_KEY.neutral }}
              >
                {pct(data.structural_pct)}
              </div>
            </div>
            <div data-testid="seat-drops-actionable">
              <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim">
                Actionable
              </div>
              <div
                className="font-mono text-base mt-0.5"
                style={{
                  color: data.actionable_pct > 0.30
                    ? COLOR_BY_KEY.warn
                    : COLOR_BY_KEY.neutral,
                }}
              >
                {pct(data.actionable_pct)}
              </div>
            </div>
          </div>

          {headline && (
            <div
              className="font-mono text-[10px] px-2 py-1.5 border"
              style={{ color: headline.color, borderColor: headline.color + "55" }}
              data-testid="seat-drops-headline"
            >
              {headline.text}
            </div>
          )}

          {/* Reasons table */}
          <div data-testid="seat-drops-reasons">
            <div className="font-mono text-[9px] uppercase tracking-widest text-rd-dim mb-1">
              By reason
            </div>
            <table className="w-full font-mono text-[10px] border-collapse">
              <thead>
                <tr className="text-rd-dim text-left">
                  <th className="py-1 px-1 text-[9px] uppercase tracking-widest font-mono">Reason</th>
                  <th className="py-1 px-1 text-right text-[9px] uppercase tracking-widest font-mono">Count</th>
                  <th className="py-1 px-1 text-right text-[9px] uppercase tracking-widest font-mono">%</th>
                  <th className="py-1 px-1 text-[9px] uppercase tracking-widest font-mono">Category</th>
                </tr>
              </thead>
              <tbody>
                {data.reasons.map((r) => (
                  <tr
                    key={r.reason}
                    className="border-t border-rd-border/30 align-top"
                    data-testid={`seat-drops-row-${r.reason}`}
                  >
                    <td className="py-1.5 px-1 text-rd-text break-all">
                      {r.reason}
                      <div className="text-rd-dim text-[9px] mt-0.5 leading-snug">
                        {r.interpretation}
                      </div>
                    </td>
                    <td className="py-1.5 px-1 text-right text-rd-text whitespace-nowrap">
                      {r.count.toLocaleString()}
                    </td>
                    <td className="py-1.5 px-1 text-right text-rd-text whitespace-nowrap">
                      {pct(r.pct)}
                    </td>
                    <td className="py-1.5 px-1">
                      <CategoryBadge category={r.category} color={r.color} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* By lane (always visible — small) */}
          {data.by_lane?.length > 0 && (
            <div className="flex flex-wrap gap-2" data-testid="seat-drops-by-lane">
              <span className="font-mono text-[9px] uppercase tracking-widest text-rd-dim self-center">
                By lane:
              </span>
              {data.by_lane.map((l) => (
                <span
                  key={l.lane}
                  className="font-mono text-[10px] text-rd-text px-1.5 py-0.5 border border-rd-border/40"
                  data-testid={`seat-drops-lane-${l.lane}`}
                >
                  {l.lane} · {l.rejected.toLocaleString()}
                </span>
              ))}
            </div>
          )}

          {/* Collapsible per-brain breakdown */}
          {data.by_brain?.length > 0 && (
            <div>
              <button
                type="button"
                onClick={() => setShowBrainBreakdown((v) => !v)}
                className="font-mono text-[10px] text-rd-dim hover:text-rd-text"
                data-testid="seat-drops-toggle-brain-breakdown"
              >
                {showBrainBreakdown ? "▾" : "▸"} By brain ({data.by_brain.length})
              </button>
              {showBrainBreakdown && (
                <table className="w-full font-mono text-[10px] mt-1 border-collapse" data-testid="seat-drops-brain-table">
                  <thead>
                    <tr className="text-rd-dim text-left">
                      <th className="py-1 px-1 text-[9px] uppercase tracking-widest font-mono">Brain</th>
                      <th className="py-1 px-1 text-right text-[9px] uppercase tracking-widest font-mono">Rejected</th>
                      <th className="py-1 px-1 text-[9px] uppercase tracking-widest font-mono">Top reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.by_brain.map((b) => (
                      <tr
                        key={b.brain}
                        className="border-t border-rd-border/30"
                        data-testid={`seat-drops-brain-row-${b.brain}`}
                      >
                        <td className="py-1 px-1 text-rd-text">{b.brain}</td>
                        <td className="py-1 px-1 text-right text-rd-text">
                          {b.rejected.toLocaleString()}
                        </td>
                        <td className="py-1 px-1 text-rd-dim break-all">{b.top_reason || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}

          {/* Collapsible per-seat breakdown */}
          {data.by_seat?.length > 0 && (
            <div>
              <button
                type="button"
                onClick={() => setShowSeatBreakdown((v) => !v)}
                className="font-mono text-[10px] text-rd-dim hover:text-rd-text"
                data-testid="seat-drops-toggle-seat-breakdown"
              >
                {showSeatBreakdown ? "▾" : "▸"} By seat ({data.by_seat.length})
              </button>
              {showSeatBreakdown && (
                <table className="w-full font-mono text-[10px] mt-1 border-collapse" data-testid="seat-drops-seat-table">
                  <thead>
                    <tr className="text-rd-dim text-left">
                      <th className="py-1 px-1 text-[9px] uppercase tracking-widest font-mono">Seat</th>
                      <th className="py-1 px-1 text-[9px] uppercase tracking-widest font-mono">Lane</th>
                      <th className="py-1 px-1 text-right text-[9px] uppercase tracking-widest font-mono">Rejected</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.by_seat.map((s) => (
                      <tr
                        key={s.seat}
                        className="border-t border-rd-border/30"
                        data-testid={`seat-drops-seat-row-${s.seat}`}
                      >
                        <td className="py-1 px-1 text-rd-text">{s.seat}</td>
                        <td className="py-1 px-1 text-rd-dim">{s.lane}</td>
                        <td className="py-1 px-1 text-right text-rd-text">
                          {s.rejected.toLocaleString()}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}

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
