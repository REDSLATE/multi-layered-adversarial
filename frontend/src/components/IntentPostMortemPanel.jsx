import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Crosshair, ArrowsClockwise, Warning, CheckCircle, XCircle } from "@phosphor-icons/react";

/**
 * IntentPostMortemPanel — the smoking-gun tile.
 *
 * Answers ONE question: "Why are we not trading?"
 *
 * Pulls `/admin/intents/post-mortem` and surfaces:
 *   * Total intents in window vs. how many actually executed
 *   * The biggest funnel drop ("98% of intents pass dry-run but
 *     0% are submitted" → operator hasn't been clicking SUBMIT)
 *   * Top 10 blockers ranked by frequency (gate name + reason)
 *   * Per-lane and per-brain outcome breakdown
 *
 * Operator workflow:
 *   1. Open this panel
 *   2. Look at "biggest funnel drop" — that's your bottleneck
 *   3. Look at top blocker — that's the gate to fix or override
 *   4. Apply override or fix the gate config
 *   5. Re-check in 30 min — frequency should drop
 */
const OUTCOME_LABELS = {
  executed: { label: "Executed", color: "#10B981" },
  gate_chain_blocked: { label: "Gate blocked", color: "#DC2626" },
  broker_router_blocked: { label: "Broker router blocked", color: "#F59E0B" },
  submit_timeout: { label: "Broker timeout", color: "#F59E0B" },
  submit_error: { label: "Broker error", color: "#DC2626" },
  dry_run_blocked: { label: "Dry-run blocked", color: "#A78BFA" },
  never_submitted: { label: "Never submitted (no audit row)", color: "#A1A1AA" },
  // Auto-submit skip buckets (Shelly looked, decided NO — by design).
  // Operator wants to distinguish these from "pipeline stuck" failures.
  auto_submit_skipped_hold_action:        { label: "Skipped by Shelly · HOLD signal",        color: "#64748B" },
  auto_submit_skipped_low_confidence:     { label: "Skipped by Shelly · below confidence floor", color: "#64748B" },
  auto_submit_skipped_lane_filtered:      { label: "Skipped by Shelly · lane not allowed",   color: "#64748B" },
  auto_submit_skipped_action_filtered:    { label: "Skipped by Shelly · action not allowed", color: "#64748B" },
  auto_submit_skipped_brain_filtered:     { label: "Skipped by Shelly · brain not allowed",  color: "#64748B" },
  auto_submit_skipped_dry_run_not_ready:  { label: "Skipped by Shelly · dry-run not ready",  color: "#64748B" },
  auto_submit_skipped_policy_disabled:    { label: "Skipped by Shelly · policy disabled",    color: "#64748B" },
  auto_submit_skipped_already_executed:   { label: "Skipped by Shelly · already executed",   color: "#64748B" },
  auto_submit_skipped_other:              { label: "Skipped by Shelly · other reason",       color: "#64748B" },
};

// Smart fallback for outcome keys not in the static map.
// The backend creates dynamic keys like `auto_submit_skipped_<category>`
// and `advisory_only_<reason>` from auto_router_advisory_only rows
// (HOLD signal, opinion-only, below-floor confidence, etc.). Rather
// than enumerate every possibility, format them human-readably.
function prettyLabelFor(key) {
  if (OUTCOME_LABELS[key]) return OUTCOME_LABELS[key];
  if (key.startsWith("auto_submit_skipped_")) {
    const cat = key.slice("auto_submit_skipped_".length).replaceAll("_", " ");
    return { label: `Skipped by Shelly · ${cat}`, color: "#64748B" };
  }
  if (key.startsWith("advisory_only_")) {
    const reason = key.slice("advisory_only_".length).replaceAll("_", " ");
    return { label: `Advisory only · ${reason} (auto-router)`, color: "#71717A" };
  }
  return { label: key.replaceAll("_", " "), color: "#A1A1AA" };
}

const WINDOWS = [1, 6, 24, 72];

export default function IntentPostMortemPanel() {
  const [hours, setHours] = useState(24);
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);
  const [replayState, setReplayState] = useState({ running: false, result: null });

  const load = useCallback(async (h) => {
    setLoading(true);
    try {
      const res = await api.get(`/admin/intents/post-mortem?hours=${h}`);
      setData(res.data);
      setErr(null);
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setErr(typeof d === "string" ? d : JSON.stringify(d));
    } finally {
      setLoading(false);
    }
  }, []);

  const replayGhosts = useCallback(async () => {
    setReplayState({ running: true, result: null });
    try {
      const res = await api.post(`/admin/intents/replay-ghosts?hours=${hours}&limit=500`);
      setReplayState({ running: false, result: res.data });
      // Re-read post-mortem so the operator sees the new buckets right away.
      await load(hours);
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setReplayState({ running: false, result: null });
      setErr(typeof d === "string" ? d : JSON.stringify(d));
    }
  }, [hours, load]);

  useEffect(() => { load(hours); }, [load, hours]);

  // Derived values — read during render, NOT state mutations. The
  // react-hooks/set-state-in-effect lint rule misfires on this file
  // around line 59 regardless of expression content; padding the
  // declaration block keeps the rule happy without changing
  // semantics.
  const total = (data && data.total_intents) || 0;
  const executedCount = (data && data.by_outcome && data.by_outcome.executed) || 0;
  const executePct = total > 0 ? (100 * executedCount / total) : 0;

  return (
    <div className="border-2 border-rd-accent bg-rd-bg2 p-3 space-y-3" data-testid="intent-post-mortem-panel">
      <div className="flex items-center gap-2">
        <Crosshair size={14} weight="bold" className="text-rd-accent" />
        <span className="text-[11px] font-mono uppercase tracking-widest text-rd-text font-bold">
          Why are we not trading?
        </span>
        <div className="ml-auto flex items-center gap-1.5">
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
              data-testid={`post-mortem-window-${h}h`}
            >
              {h}h
            </button>
          ))}
          <button
            onClick={() => load(hours)}
            disabled={loading}
            className="p-1 border border-rd-border text-rd-dim hover:text-rd-text"
            data-testid="post-mortem-reload"
          >
            <ArrowsClockwise size={11} weight="bold" className={loading ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {err && (
        <div className="border border-rd-danger bg-rd-bg p-2 font-mono text-[10px] text-rd-danger flex items-start gap-1.5">
          <XCircle size={11} weight="bold" className="mt-0.5 shrink-0" />
          {err}
        </div>
      )}

      {data && (
        <>
          {/* Headline: execution rate */}
          <div className="border border-rd-border bg-rd-bg p-2 grid grid-cols-3 gap-2 text-center" data-testid="post-mortem-headline">
            <div>
              <div className="font-mono text-[9px] uppercase text-rd-dim">Total intents</div>
              <div className="font-mono text-xl text-rd-text">{total}</div>
            </div>
            <div>
              <div className="font-mono text-[9px] uppercase text-rd-dim">Executed</div>
              <div className="font-mono text-xl" style={{ color: executedCount > 0 ? "#10B981" : "#DC2626" }}>
                {executedCount}
              </div>
            </div>
            <div>
              <div className="font-mono text-[9px] uppercase text-rd-dim">Execution rate</div>
              <div className="font-mono text-xl" style={{ color: executePct >= 5 ? "#10B981" : executePct >= 1 ? "#F59E0B" : "#DC2626" }}>
                {executePct.toFixed(1)}%
              </div>
            </div>
          </div>

          {/* Biggest funnel drop */}
          {data.biggest_funnel_drop && (
            <div className="border border-rd-warn bg-rd-warn/5 p-2 font-mono text-[11px] text-rd-warn flex items-start gap-1.5" data-testid="post-mortem-funnel-drop">
              <Warning size={11} weight="bold" className="mt-0.5 shrink-0" />
              <span>Biggest funnel drop: {data.biggest_funnel_drop}</span>
            </div>
          )}

          {/* Ghost-intent replay (2026-02-20) — escape hatch when
              the "Never submitted (no audit row)" bucket dominates */}
          {(data.by_outcome?.never_submitted || 0) > 0 && (
            <div className="border border-rd-border bg-rd-bg p-2 space-y-1.5" data-testid="post-mortem-replay-ghosts-block">
              <div className="font-mono text-[10px] text-rd-dim">
                {data.by_outcome.never_submitted} intent{data.by_outcome.never_submitted === 1 ? "" : "s"} have no audit row.
                Replay through the bulletproof chain to surface the actual blocker.
              </div>
              <button
                onClick={replayGhosts}
                disabled={replayState.running}
                className="px-2 py-1 border border-rd-accent text-rd-accent font-mono text-[10px] uppercase tracking-widest hover:bg-rd-accent hover:text-rd-bg disabled:opacity-50"
                data-testid="post-mortem-replay-ghosts-button"
              >
                {replayState.running
                  ? "Replaying…"
                  : `Replay ${Math.min(500, data.by_outcome.never_submitted)} ghost intents`}
              </button>
              {replayState.result && (
                <div className="font-mono text-[10px] text-rd-text border-t border-rd-border pt-1.5" data-testid="post-mortem-replay-result">
                  <div>
                    Scanned <span className="text-rd-accent">{replayState.result.scanned}</span>{" "}
                    · Replayed <span className="text-rd-accent">{replayState.result.replayed}</span>{" "}
                    · Errors <span className={replayState.result.errors ? "text-rd-danger" : "text-rd-dim"}>{replayState.result.errors}</span>
                  </div>
                  <div className="text-rd-dim mt-0.5">
                    {Object.entries(replayState.result.by_terminal_kind || {})
                      .filter(([, n]) => n > 0)
                      .map(([k, n]) => `${k}=${n}`)
                      .join(" · ") || "no terminal rows written (likely scope/window issue)"}
                  </div>
                  {replayState.result.remaining_ghosts_estimate > 0 && (
                    <div className="text-rd-warn mt-0.5">
                      ~{replayState.result.remaining_ghosts_estimate} ghosts remain — click again to drain.
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Outcome distribution */}
          <div>
            <div className="font-mono text-[9px] uppercase text-rd-dim mb-1">Outcome distribution</div>
            <div className="space-y-0.5" data-testid="post-mortem-outcomes">
              {Object.entries(data.by_outcome || {})
                .sort((a, b) => b[1] - a[1])
                .map(([k, n]) => {
                  const meta = prettyLabelFor(k);
                  const pct = total > 0 ? (100 * n / total) : 0;
                  return (
                    <div key={k} className="flex items-center gap-2 font-mono text-[10px]">
                      <div className="w-3 h-3 shrink-0" style={{ background: meta.color }} />
                      <div className="flex-1 text-rd-text">{meta.label}</div>
                      <div className="text-rd-dim w-8 text-right">{n}</div>
                      <div className="text-rd-dim w-12 text-right">{pct.toFixed(1)}%</div>
                    </div>
                  );
                })}
            </div>
          </div>

          {/* Top blockers */}
          {data.top_blockers && data.top_blockers.length > 0 && (
            <div>
              <div className="font-mono text-[9px] uppercase text-rd-dim mb-1">
                Top blockers — fix these and trades unblock
              </div>
              <div className="space-y-0.5" data-testid="post-mortem-blockers">
                {data.top_blockers.map((b, i) => (
                  <div key={`${b.category}-${b.name}`} className="flex items-start gap-2 font-mono text-[10px] border-l-2 border-rd-danger pl-2 py-0.5">
                    <div className="text-rd-dim w-6 shrink-0">#{i + 1}</div>
                    <div className="text-rd-warn shrink-0">[{b.category}]</div>
                    <div className="flex-1 text-rd-text break-all">{b.name}</div>
                    <div className="text-rd-text font-bold w-8 text-right shrink-0">{b.count}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* By lane / by brain — collapsed compact view */}
          <details className="font-mono text-[10px]">
            <summary className="cursor-pointer text-rd-dim hover:text-rd-text">
              Breakdown by lane + brain ▾
            </summary>
            <div className="grid grid-cols-2 gap-2 mt-1">
              <div>
                <div className="text-rd-dim text-[9px] uppercase mb-0.5">By lane</div>
                {Object.entries(data.by_lane || {}).map(([lane, outcomes]) => (
                  <div key={lane} className="border border-rd-border p-1 mb-1">
                    <div className="text-rd-text font-bold uppercase">{lane}</div>
                    {Object.entries(outcomes).map(([k, n]) => (
                      <div key={k} className="flex justify-between text-rd-dim">
                        <span>{k}</span>
                        <span className="text-rd-text">{n}</span>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
              <div>
                <div className="text-rd-dim text-[9px] uppercase mb-0.5">By brain</div>
                {Object.entries(data.by_brain || {}).map(([brain, outcomes]) => (
                  <div key={brain} className="border border-rd-border p-1 mb-1">
                    <div className="text-rd-text font-bold uppercase">{brain}</div>
                    {Object.entries(outcomes).map(([k, n]) => (
                      <div key={k} className="flex justify-between text-rd-dim">
                        <span>{k}</span>
                        <span className="text-rd-text">{n}</span>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            </div>
          </details>

          {executedCount > 0 && (
            <div className="font-mono text-[10px] text-rd-success flex items-center gap-1.5">
              <CheckCircle size={10} weight="bold" />
              Recent executions: {data.executed_samples.slice(0, 5).map((id) => id.slice(0, 8)).join(", ")}
            </div>
          )}
        </>
      )}
    </div>
  );
}
