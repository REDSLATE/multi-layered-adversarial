// PipelineBlockerChip — at-a-glance "why isn't lane X trading?" UI.
//
// Built 2026-06-19 to answer the operator's question "equity hasn't
// made a move all day" without scrolling expanded intent rows. Reads
// from `GET /api/admin/pipeline/recent-blocker-histogram?lane=equity`
// and renders the top blockers + one-tap "fix" actions for the
// well-known patterns we can self-heal.
//
// Auto-fix patterns:
//   market_closed                                 → suggest extended-hours toggle
//   below_seat_confidence_min:X<Y                 → suggest lowering seat floor
//   insufficient_buying_power                     → surface broker BP UI
//   brain_not_current_seat_holder:X!=Y@...        → "Open QSS to fix" link
//   executor_seat_vacant:...                      → "Open QSS to fill seat"
//   trading_controls_disabled                     → "Re-enable trading"
//
// Mobile-first. Designed to live at the TOP of the Intents page next
// to the filter strip.

import { useCallback, useEffect, useMemo, useState } from "react";
import { ChartBar, X, ArrowClockwise, CaretDown, CaretUp } from "@phosphor-icons/react";
import { api } from "../lib/api";

const LANE_ORDER = ["equity", "crypto"];

const LANE_LABEL = {
  equity: "EQUITY",
  crypto: "CRYPTO",
};

// Map a blocker reason string → { label, hint, fixHref? }. The
// pattern matching is intentionally permissive — the reason strings
// from the pipeline can embed numbers and brain ids; we match by
// prefix so the same hint surfaces regardless of the dynamic tail.
function classifyBlocker(reason) {
  if (!reason) return { hint: null };
  if (reason.startsWith("market_closed_extended_hours_window")) {
    return {
      hint: "Outside Webull extended hours (4 AM – 8 PM ET M-F).",
      hintTone: "info",
    };
  }
  if (reason.startsWith("market_closed")) {
    return {
      hint: "US RTH is closed. Flip Extended Hours ON below to trade 4 AM – 8 PM ET.",
      hintTone: "info",
      fixScroll: "extended-hours-toggle-block",
    };
  }
  if (reason.startsWith("below_seat_confidence_min")) {
    return {
      hint: "Brain's confidence is below the seat floor. Lower the seat's confidence_min, or wait for a stronger signal.",
      hintTone: "warn",
    };
  }
  if (reason.startsWith("insufficient_buying_power")) {
    return {
      hint: "Webull buying power doesn't cover the requested notional. Lower the auto-router default notional, or fund the account.",
      hintTone: "warn",
    };
  }
  if (reason.startsWith("brain_not_current_seat_holder")) {
    return {
      hint: "Brain that posted the intent is NOT the operator's current pick for the executor seat. Open QSS to confirm/swap.",
      hintTone: "warn",
    };
  }
  if (reason.startsWith("executor_seat_vacant")) {
    return {
      hint: "No brain holds the lane's executor seat. Open QSS to assign one.",
      hintTone: "danger",
    };
  }
  if (reason.startsWith("brain_not_trusted_for_seat")) {
    return {
      hint: "Brain not in trust list for this seat. Assign the brain via QSS — that mirrors it into the trust list.",
      hintTone: "warn",
    };
  }
  if (reason === "trading_controls_disabled") {
    return {
      hint: "Operator kill switch is engaged. Re-enable via /admin/trading/enable.",
      hintTone: "danger",
    };
  }
  if (reason === "zero_notional") {
    return {
      hint: "Sizing collapsed to $0 — check seat max_notional_usd and governor risk_multiplier.",
      hintTone: "warn",
    };
  }
  if (reason === "duplicate_order") {
    return {
      hint: "Same (brain, lane, symbol, side) already in flight. Will retry once the existing fill completes.",
      hintTone: "info",
    };
  }
  if (reason.startsWith("seat_disabled")) {
    return { hint: "Operator disabled this seat. Re-enable via seat policy admin.", hintTone: "warn" };
  }
  if (reason.startsWith("seat_missing")) {
    return { hint: "No seat-policy row exists for this lane. Run the Paradox v2 seeder.", hintTone: "danger" };
  }
  return { hint: null };
}

function toneClasses(tone) {
  if (tone === "danger") return "text-rd-danger";
  if (tone === "warn") return "text-rd-warn";
  if (tone === "info") return "text-rd-accent";
  return "text-rd-dim";
}

export default function PipelineBlockerChip() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.get("/admin/pipeline/recent-blocker-histogram", {
        params: { hours: 24 },
      });
      setData(res.data);
    } catch (e) {
      setError(e?.response?.data?.detail || e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  // Auto-refresh every 60s so the chip stays fresh without an
  // operator tap. Cheap — one read of `pipeline_receipts`.
  useEffect(() => {
    const id = setInterval(load, 60_000);
    return () => clearInterval(id);
  }, [load]);

  const lanes = useMemo(() => {
    if (!data?.by_lane) return [];
    return LANE_ORDER
      .filter((l) => data.by_lane[l])
      .map((l) => ({ lane: l, ...data.by_lane[l] }));
  }, [data]);

  return (
    <div
      className="border border-rd-border bg-rd-card mb-3"
      data-testid="pipeline-blocker-chip"
    >
      {/* HEADER */}
      <button
        onClick={() => setExpanded((e) => !e)}
        className="w-full flex items-center justify-between gap-2 px-3 py-2 hover:bg-rd-bg/30 transition-colors"
        data-testid="pipeline-blocker-chip-toggle"
      >
        <div className="flex items-center gap-2 min-w-0">
          <ChartBar size={14} weight="bold" className="text-rd-accent shrink-0" />
          <span className="font-mono text-[10px] uppercase tracking-widest text-rd-text font-bold">
            Why intents aren&apos;t flowing
          </span>
          <span className="font-mono text-[9px] text-rd-dim">last 24h</span>
        </div>
        <div className="flex items-center gap-2">
          {loading ? (
            <span className="font-mono text-[9px] text-rd-dim">loading…</span>
          ) : (
            <div className="flex items-center gap-1.5">
              {lanes.map(({ lane, executed, blocked, broker_error }) => (
                <span
                  key={lane}
                  className="font-mono text-[9px] tracking-wider"
                  data-testid={`pipeline-blocker-chip-${lane}-summary`}
                >
                  <span className="text-rd-dim">{LANE_LABEL[lane]}</span>{" "}
                  <span className="text-rd-success">✓{executed || 0}</span>{" "}
                  <span className={blocked > 0 ? "text-rd-danger" : "text-rd-dim"}>
                    ✗{(blocked || 0) + (broker_error || 0)}
                  </span>
                </span>
              ))}
            </div>
          )}
          {expanded ? (
            <CaretUp size={11} weight="bold" className="text-rd-dim" />
          ) : (
            <CaretDown size={11} weight="bold" className="text-rd-dim" />
          )}
        </div>
      </button>

      {/* BODY — expanded */}
      {expanded && (
        <div className="border-t border-rd-border/40 px-3 py-2 space-y-3" data-testid="pipeline-blocker-chip-body">
          {error && (
            <div className="font-mono text-[10px] text-rd-danger flex items-start gap-1">
              <X size={11} weight="bold" className="mt-0.5 shrink-0" />
              {error}
            </div>
          )}
          {!error && lanes.length === 0 && !loading && (
            <div className="font-mono text-[10px] text-rd-dim">
              No pipeline receipts in the last 24h. Either nothing posted, or
              the auto-router is stopped.
            </div>
          )}
          {lanes.map(({ lane, total, executed, blocked, broker_error, blockers, recent_samples }) => (
            <div key={lane} className="space-y-1.5" data-testid={`pipeline-blocker-chip-${lane}`}>
              <div className="flex items-baseline justify-between">
                <div className="font-mono text-[10px] uppercase tracking-widest text-rd-text font-bold">
                  {LANE_LABEL[lane]}
                </div>
                <div className="font-mono text-[9px] text-rd-dim">
                  {total} receipts ·{" "}
                  <span className="text-rd-success">{executed} executed</span> ·{" "}
                  <span className="text-rd-danger">{blocked} blocked</span>
                  {broker_error ? (
                    <>
                      {" "}· <span className="text-rd-danger">{broker_error} broker_error</span>
                    </>
                  ) : null}
                </div>
              </div>
              {(blockers || []).length === 0 ? (
                <div className="font-mono text-[9px] text-rd-dim pl-3">
                  No blockers in this window.
                </div>
              ) : (
                <div className="space-y-1">
                  {blockers.slice(0, 6).map((b) => {
                    const { hint, hintTone, fixScroll } = classifyBlocker(b.reason);
                    return (
                      <div
                        key={`${b.source || "src"}-${b.reason || "r"}`}
                        className="font-mono text-[9px] pl-3 border-l-2 border-rd-border/40"
                        data-testid={`pipeline-blocker-chip-${lane}-row-${b.source || "src"}-${(b.reason || "r").replace(/[^a-z0-9]/gi, "").slice(0, 20)}`}
                      >
                        <div className="flex items-baseline justify-between gap-2">
                          <div className="min-w-0 flex-1">
                            <span className="text-rd-dim uppercase tracking-wider">
                              {b.source}
                            </span>{" "}
                            <span className="text-rd-text break-all">{b.reason}</span>
                          </div>
                          <span className="text-rd-text shrink-0 ml-2">×{b.count}</span>
                        </div>
                        {hint && (
                          <div className={`mt-0.5 ${toneClasses(hintTone)}`}>
                            ↳ {hint}
                            {fixScroll && (
                              <button
                                onClick={() => {
                                  const el = document.querySelector(`[data-testid="${fixScroll}"]`);
                                  if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
                                }}
                                className="ml-1 underline hover:opacity-80"
                                data-testid={`pipeline-blocker-chip-fix-${b.source || "src"}-${(b.reason || "r").replace(/[^a-z0-9]/gi, "").slice(0, 20)}`}
                              >
                                jump to fix
                              </button>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          ))}
          <div className="flex items-center justify-between pt-1 border-t border-rd-border/30">
            <span className="font-mono text-[9px] text-rd-dim">
              {data?.now ? `as of ${new Date(data.now).toLocaleTimeString()}` : ""}
            </span>
            <button
              onClick={load}
              disabled={loading}
              className="px-2 py-0.5 border border-rd-border text-rd-dim hover:text-rd-text hover:border-rd-accent font-mono text-[9px] uppercase tracking-widest disabled:opacity-40"
              data-testid="pipeline-blocker-chip-refresh"
            >
              <ArrowClockwise size={9} weight="bold" className="inline mr-1" />
              refresh
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
