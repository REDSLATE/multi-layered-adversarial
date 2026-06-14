import React, { useState } from "react";
import { api, relTime } from "@/lib/api";
import { useCouncilLive } from "./useCouncilLive";
import {
  ArrowsClockwise, CheckCircle, Warning, XCircle, Eye,
} from "@phosphor-icons/react";

/**
 * CouncilChamberTile — operator's real-time, four-column view of the
 * Paradox v2 brain council.
 *
 * Each column is one brain. The latest BrainVote per brain is shown
 * at a glance: stance, symbol, regime, calibrated confidence, when.
 * A quorum indicator at the top says how many of the four brains
 * have spoken in the last 10 minutes — distinguishes a SILENT brain
 * from a HOLDing one.
 *
 * Polls /api/v2/council/live every 6s. Stops polling when the page
 * is hidden so it doesn't burn the operator's bandwidth.
 *
 * No mutate actions — pure read surface. Operator-only.
 */

const STANCE_COLOR = {
  BUY:     "text-rd-success",
  SELL:    "text-rd-danger",
  HOLD:    "text-rd-dim",
  ABSTAIN: "text-rd-warn",
};

const BRAIN_DOCTRINE = {
  alpha:    "adversarial",
  camaro:   "tape reading",
  chevelle: "trend",
  redeye:   "mean reversion",
};

function StanceBadge({ stance }) {
  const cls = STANCE_COLOR[stance] || "text-rd-dim";
  return (
    <span
      className={`inline-block font-mono text-[13px] font-bold tracking-wider ${cls}`}
      data-testid={`council-stance-${stance?.toLowerCase()}`}
    >
      {stance || "—"}
    </span>
  );
}

function BrainColumn({ brain }) {
  const { brain_id, display_name, latest } = brain;
  const silent = !latest;
  const ts = latest?.timestamp;
  const symbol = latest?.symbol || "—";
  const regime = latest?.regime || "—";
  const confCal = latest?.calibrated_confidence;
  const stance = latest?.stance;
  const negKnow = latest?.negative_knowledge_triggered;

  return (
    <div
      className={`flex flex-col gap-2 p-3 border ${
        silent ? "border-rd-warn/40 bg-rd-warn/5" : "border-rd-border bg-rd-bg"
      } min-h-[140px]`}
      data-testid={`council-column-${brain_id}`}
    >
      {/* Header: brain identity */}
      <div className="flex items-baseline justify-between">
        <div>
          <div className="font-mono text-[12px] font-bold text-rd-text uppercase tracking-wider">
            {display_name}
          </div>
          <div className="font-mono text-[9px] text-rd-dim uppercase tracking-wider">
            {brain_id} · {BRAIN_DOCTRINE[brain_id] || "—"}
          </div>
        </div>
        {silent ? (
          <span className="text-rd-warn" title="No vote in 10 min window">
            <Warning size={14} weight="bold" />
          </span>
        ) : negKnow ? (
          <span className="text-rd-warn" title="Negative knowledge fired — vote forced to ABSTAIN">
            <XCircle size={14} weight="bold" />
          </span>
        ) : (
          <span className="text-rd-success" title="Alive">
            <CheckCircle size={14} weight="bold" />
          </span>
        )}
      </div>

      {/* Stance + symbol */}
      <div className="flex items-baseline justify-between">
        <StanceBadge stance={stance} />
        <span className="font-mono text-[11px] text-rd-text" data-testid={`council-symbol-${brain_id}`}>
          {symbol}
        </span>
      </div>

      {/* Regime + confidence */}
      <div className="flex items-baseline justify-between font-mono text-[10px]">
        <span className="text-rd-dim uppercase">{regime}</span>
        <span className="text-rd-text">
          {typeof confCal === "number" ? `c=${confCal.toFixed(2)}` : "c=—"}
        </span>
      </div>

      {/* Reasoning (first line only — keep terse) */}
      {latest?.reasoning?.[0] && (
        <div className="font-mono text-[9px] text-rd-dim italic line-clamp-2" title={latest.reasoning.join(" · ")}>
          {latest.reasoning[0]}
        </div>
      )}

      {/* When */}
      <div className="mt-auto font-mono text-[9px] text-rd-dim">
        {ts ? relTime(ts) : "never spoken"}
      </div>
    </div>
  );
}

export default function CouncilChamberTile() {
  const { data, err, loading, refresh } = useCouncilLive();
  const [busy, setBusy] = useState(false);

  const onRefresh = async () => {
    setBusy(true);
    try { await refresh(); } finally { setBusy(false); }
  };

  if (err) {
    return (
      <div className="border border-rd-danger/50 bg-rd-danger/5 p-3 font-mono text-[10px] text-rd-danger" data-testid="council-error">
        Council Chamber load failed: {String(err.message || err)}
      </div>
    );
  }

  const chamber = data?.chamber || [];
  const quorum = data?.quorum || { alive_count: 0, expected: 4, alive_in_10min: [] };
  const allAlive = quorum.alive_count === quorum.expected;

  return (
    <div className="space-y-2 p-3 border border-rd-border bg-rd-bg2" data-testid="council-chamber-tile">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Eye size={12} weight="bold" className="text-rd-accent" />
            <span className="font-mono text-[11px] font-bold text-rd-text uppercase tracking-widest">
              Council Chamber
            </span>
            <span className="font-mono text-[9px] text-rd-dim uppercase">
              · live brain votes
            </span>
          </div>
          <div className="font-mono text-[9px] text-rd-dim mt-1">
            Per Paradox v2 doctrine: brains emit opinions only; seats decide execution. This tile shows the four canonical brains{"'"} latest stance.
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`font-mono text-[10px] ${allAlive ? "text-rd-success" : "text-rd-warn"}`}
            data-testid="council-quorum"
          >
            quorum: {quorum.alive_count}/{quorum.expected} · 10m
          </span>
          <button
            onClick={onRefresh}
            disabled={busy || loading}
            className="text-rd-dim hover:text-rd-text disabled:opacity-50"
            data-testid="council-refresh"
            title="Refresh"
          >
            <ArrowsClockwise size={11} weight="bold" className={busy ? "animate-spin" : ""} />
          </button>
        </div>
      </div>

      {/* Four columns */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-2">
        {loading && chamber.length === 0 ? (
          <div className="col-span-full font-mono text-[10px] text-rd-dim" data-testid="council-loading">
            loading council …
          </div>
        ) : (
          chamber.map((b) => <BrainColumn key={b.brain_id} brain={b} />)
        )}
      </div>
    </div>
  );
}
