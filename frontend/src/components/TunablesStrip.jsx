import React, { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Sliders, ArrowsClockwise } from "@phosphor-icons/react";

/**
 * TunablesStrip — what-if simulator for the auto-submit policy.
 *
 * Pure read tile. Polls /api/admin/auto-submit/tunables-simulator
 * every 30s. Shows three rows the operator can scan in < 5 seconds:
 *
 *   • Skip distribution         → "3500 HOLD · 156 low_conf · 12 lane"
 *   • Confidence what-if        → "0.85 → 0.75 unlocks 87 (NVDA 35, AAL 28)"
 *   • Action / lane what-if     → only if the operator's current
 *                                 allowed_actions / allowed_lanes are
 *                                 leaving something on the table.
 *
 * No mutate actions. Operator sees the cost of loosening a filter
 * BEFORE they commit to loosening it via the policy panel.
 */

const fmtPairs = (pairs, max = 3) => {
  if (!pairs?.length) return "—";
  return pairs.slice(0, max).map(([k, v]) => `${k} ${v}`).join(" · ");
};

const SKIP_CAT_LABELS = {
  hold_action:        "HOLD",
  low_confidence:     "low-conf",
  lane_filtered:      "lane",
  action_filtered:    "action",
  brain_filtered:     "brain",
  dry_run_not_ready:  "dry-run",
  dry_run_blocked:    "dry-blk",
  dry_run_pending:    "dry-pend",
  dry_run_missing:    "dry-miss",
  policy_disabled:    "off",
  already_executed:   "racy",
  other:              "other",
};

export default function TunablesStrip() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const r = await api.get("/admin/auto-submit/tunables-simulator?hours=24");
      setData(r.data);
      setErr(null);
    } catch (e) {
      const d = e?.response?.data?.detail || e.message;
      setErr(typeof d === "string" ? d : JSON.stringify(d));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      if (!alive) return;
      await load();
      if (alive) setTimeout(tick, 30000);
    };
    tick();
    return () => { alive = false; };
  }, [load]);

  if (err) {
    return (
      <div className="border border-rd-warn/40 bg-rd-warn/5 p-2 font-mono text-[10px] text-rd-warn" data-testid="tunables-strip-error">
        tunables simulator failed: {err}
      </div>
    );
  }
  if (!data) {
    return (
      <div className="border border-rd-border bg-rd-bg2 p-2 font-mono text-[10px] text-rd-dim" data-testid="tunables-strip-loading">
        loading what-if simulator …
      </div>
    );
  }

  const skipBreakdown = Object.entries(data.by_skip_category || {})
    .sort(([, a], [, b]) => b - a)
    .slice(0, 5)
    .map(([k, v]) => `${v} ${SKIP_CAT_LABELS[k] || k}`)
    .join(" · ");

  const confidenceRows = (data.confidence_what_if || []).slice(0, 3);
  const laneRows       = (data.lane_what_if || []).slice(0, 3);
  const actionRows     = (data.action_what_if || []).slice(0, 3);

  return (
    <div className="border border-rd-border bg-rd-bg2 p-3 font-mono text-[10px] space-y-1.5" data-testid="tunables-strip">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <Sliders size={11} weight="bold" className="text-rd-accent" />
          <span className="text-[10px] font-bold uppercase tracking-widest text-rd-text">
            What-if · auto-submit dial
          </span>
          <span className="text-[9px] text-rd-dim uppercase">· last 24h</span>
        </div>
        <button
          onClick={load}
          disabled={busy}
          className="text-rd-dim hover:text-rd-text disabled:opacity-50"
          title="refresh"
          data-testid="tunables-refresh"
        >
          <ArrowsClockwise size={11} weight="bold" className={busy ? "animate-spin" : ""} />
        </button>
      </div>

      {/* Skip distribution */}
      <div data-testid="tunables-skip-distribution">
        <span className="text-[9px] text-rd-dim uppercase">Skips today: </span>
        <span className="text-rd-text">{data.total_skipped}</span>
        <span className="text-rd-dim"> · </span>
        <span className="text-rd-text">{skipBreakdown || "—"}</span>
      </div>

      {/* Confidence what-if */}
      {confidenceRows.length > 0 && (
        <div data-testid="tunables-confidence-whatif" className="space-y-0.5">
          <div className="text-[9px] text-rd-dim uppercase">
            Lower confidence_min from {data.current_confidence_min}:
          </div>
          {confidenceRows.map((row) => (
            <div key={row.new_min} className="pl-2" data-testid={`tunables-conf-${row.new_min}`}>
              <span className="text-rd-accent font-bold">→ {row.new_min}</span>
              <span className="text-rd-dim"> unlocks </span>
              <span className="text-rd-text font-bold">{row.would_unlock}</span>
              <span className="text-rd-dim"> · symbols: </span>
              <span className="text-rd-text">{fmtPairs(row.top_symbols)}</span>
              {row.top_brains?.length > 0 && (
                <>
                  <span className="text-rd-dim"> · brains: </span>
                  <span className="text-rd-text">{fmtPairs(row.top_brains)}</span>
                </>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Lane what-if — only show if there's untapped lanes */}
      {laneRows.length > 0 && (
        <div data-testid="tunables-lane-whatif" className="space-y-0.5">
          <div className="text-[9px] text-rd-dim uppercase">
            Add lane to allowed_lanes [{data.current_allowed_lanes.join(", ")}]:
          </div>
          {laneRows.map((row) => (
            <div key={row.lane} className="pl-2" data-testid={`tunables-lane-${row.lane}`}>
              <span className="text-rd-accent font-bold">+ {row.lane}</span>
              <span className="text-rd-dim"> unlocks </span>
              <span className="text-rd-text font-bold">{row.would_unlock}</span>
              <span className="text-rd-dim"> · </span>
              <span className="text-rd-text">{fmtPairs(row.top_symbols)}</span>
            </div>
          ))}
        </div>
      )}

      {/* Action what-if */}
      {actionRows.length > 0 && (
        <div data-testid="tunables-action-whatif" className="space-y-0.5">
          <div className="text-[9px] text-rd-dim uppercase">
            Add action to allowed_actions [{data.current_allowed_actions.join(", ")}]:
          </div>
          {actionRows.map((row) => (
            <div key={row.action} className="pl-2" data-testid={`tunables-action-${row.action}`}>
              <span className="text-rd-accent font-bold">+ {row.action}</span>
              <span className="text-rd-dim"> unlocks </span>
              <span className="text-rd-text font-bold">{row.would_unlock}</span>
              <span className="text-rd-dim"> · </span>
              <span className="text-rd-text">{fmtPairs(row.top_symbols)}</span>
            </div>
          ))}
        </div>
      )}

      {/* Empty state */}
      {confidenceRows.length === 0 && laneRows.length === 0 && actionRows.length === 0 && (
        <div className="text-rd-dim italic" data-testid="tunables-no-suggestions">
          no actionable suggestions — every dial is already producing nothing-to-unlock today.
        </div>
      )}

      <div className="text-[9px] text-rd-dim italic pt-1 border-t border-rd-border">
        Read-only what-if. To apply, edit policy in the Auto-Submit Policy panel above.
      </div>
    </div>
  );
}
