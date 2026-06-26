/**
 * ExecutionScoreBreakdown — "Why blocked? How close?" math for one intent.
 *
 * Synthesizes the doctrine packet's per-seat advisory fields into a
 * single 0..1 execution score so the operator can answer:
 *
 *   "Was this blocked because we're 5% too conservative, or because
 *    the doctrine is genuinely 50% against the trade?"
 *
 * Math (transparent, derived from existing doctrine packet fields —
 * NOT a separate gate; the actual block reason still lives in
 * `failing_gates` on the intent):
 *
 *   start                 = 1.00
 *   strategist_penalty    = max(0, -strategist_conviction_delta)
 *   adversary_penalty     = objection_count * 0.05 +
 *                           (challenge_strength * 0.20 if required else 0)
 *   governor_penalty      = max(0, 1.0 - governor_risk_multiplier)
 *                           (only counts if governor_action ≠ NORMAL)
 *   doctrine_reject_pen   = (1.0 - doctrine_score) if quality=REJECT
 *
 *   final_score = clamp01(1.0 - Σ penalties)
 *
 * Threshold is intentionally surfaced but not enforced — it's a
 * read of the operator's `required_for_execution_threshold`
 * tunable (defaults to 0.50). Adjusting it does NOT change which
 * intents execute (the gate chain does that). It just changes the
 * "missed by" framing here.
 *
 * Doctrine pin (2026-02-23): this is a DIAGNOSTIC view. The actual
 * decision logic remains in the gate chain. This panel exists to
 * tell the operator "the doctrine layer thinks this trade is bad
 * by X%" so they know whether to tune the doctrine or retire it.
 */
const DEFAULT_THRESHOLD = 0.50;

function clamp01(v) {
  if (!Number.isFinite(v)) return 0;
  return Math.max(0, Math.min(1, v));
}

function pct(n) {
  return `${Math.round(n * 100)}%`;
}

function computeBreakdown(packet) {
  if (!packet) return null;
  const quality   = packet.quality || null;
  const score     = Number.isFinite(packet.score) ? packet.score : null;
  const stratΔ    = Number.isFinite(packet.strategist_conviction_delta)
    ? packet.strategist_conviction_delta : 0;
  const objs      = Number.isFinite(packet.adversary_objection_count)
    ? packet.adversary_objection_count : 0;
  const cs        = Number.isFinite(packet.adversary_challenge_strength)
    ? packet.adversary_challenge_strength : 0;
  const required  = !!packet.adversary_challenge_required;
  const govAction = packet.governor_action || "NORMAL";
  const govRisk   = Number.isFinite(packet.governor_risk_multiplier)
    ? packet.governor_risk_multiplier : 1.0;

  const strategist_penalty = Math.max(0, -stratΔ);
  const adversary_penalty  = objs * 0.05 + (required ? cs * 0.20 : 0);
  const governor_penalty   = govAction !== "NORMAL"
    ? Math.max(0, 1.0 - govRisk) : 0;
  const doctrine_penalty   = (quality === "REJECT" && score != null)
    ? Math.max(0, 1.0 - score) : 0;

  const total_penalty = strategist_penalty + adversary_penalty +
    governor_penalty + doctrine_penalty;
  const final = clamp01(1.0 - total_penalty);

  return {
    final,
    components: [
      { label: "strategist conviction", penalty: strategist_penalty,
        detail: stratΔ != null ? `Δ=${stratΔ.toFixed(2)}` : "" },
      { label: "adversary objections",  penalty: adversary_penalty,
        detail: `${objs} obj${objs === 1 ? "" : "s"}${
          required ? `, cs=${cs.toFixed(2)} required` : ""}` },
      { label: "governor risk",         penalty: governor_penalty,
        detail: `${govAction}, mult=${govRisk.toFixed(2)}` },
      { label: "doctrine quality",      penalty: doctrine_penalty,
        detail: quality
          ? `${quality}${score != null ? ` score=${score.toFixed(2)}` : ""}`
          : "n/a" },
    ],
    total_penalty,
  };
}

export default function ExecutionScoreBreakdown({
  intent,
  threshold = DEFAULT_THRESHOLD,
}) {
  const packet = intent?.doctrine_packet;
  const breakdown = computeBreakdown(packet);
  const failingGates = intent?.failing_gates || [];

  if (!breakdown) {
    return (
      <div
        className="text-[11px] font-mono text-slate-500"
        data-testid={`exec-score-${intent?.intent_id || "unknown"}-empty`}
      >
        no doctrine packet — execution score unavailable
      </div>
    );
  }

  const missedBy = breakdown.final < threshold ? threshold - breakdown.final : 0;
  const cleared = breakdown.final >= threshold;
  const tone = cleared ? "text-emerald-300" : missedBy < 0.10
    ? "text-amber-300"  // close miss
    : "text-red-300";

  return (
    <div
      className="space-y-2 text-[11px]"
      data-testid={`exec-score-${intent?.intent_id || "unknown"}`}
    >
      <div className="flex items-baseline gap-4 font-mono">
        <span className="text-slate-400 uppercase tracking-wider text-[10px]">
          execution score
        </span>
        <span
          className={`text-base font-semibold ${tone}`}
          data-testid={`exec-score-${intent?.intent_id}-final`}
        >
          {pct(breakdown.final)}
        </span>
        <span className="text-slate-500">
          threshold {pct(threshold)}
        </span>
        {missedBy > 0 && (
          <span
            className="text-red-300 font-semibold"
            data-testid={`exec-score-${intent?.intent_id}-missed-by`}
          >
            missed by {pct(missedBy)}
          </span>
        )}
        {cleared && (
          <span className="text-emerald-300">cleared advisory</span>
        )}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-1.5 font-mono">
        {breakdown.components.map((c) => (
          <div
            key={c.label}
            className="bg-slate-950/40 border border-slate-800 rounded px-2 py-1.5"
            data-testid={`exec-score-component-${c.label.replace(/\s+/g, "-")}`}
          >
            <div className="text-[9px] uppercase tracking-wider text-slate-500">
              {c.label}
            </div>
            <div className={`text-sm ${c.penalty > 0 ? "text-red-300" : "text-slate-300"}`}>
              {c.penalty > 0 ? `−${pct(c.penalty)}` : "0%"}
            </div>
            <div className="text-[9px] text-slate-500 truncate" title={c.detail}>
              {c.detail}
            </div>
          </div>
        ))}
      </div>

      {failingGates.length > 0 && (
        <div className="pt-2 border-t border-slate-800">
          <div className="text-[10px] uppercase tracking-wider text-slate-500 mb-1">
            actual block (gate chain)
          </div>
          <div className="space-y-1">
            {failingGates.slice(0, 3).map((g, i) => (
              <div
                key={`${g.name || g.gate || i}-${i}`}
                className="font-mono text-[10px] text-red-300"
                data-testid={`exec-score-failing-gate-${g.name || g.gate || i}`}
              >
                <span className="text-slate-400">└─</span>{" "}
                <span className="text-amber-300">{g.name || g.gate || "?"}</span>
                {g.reason ? ` — ${g.reason}` : ""}
              </div>
            ))}
            {failingGates.length > 3 && (
              <div className="font-mono text-[10px] text-slate-500">
                + {failingGates.length - 3} more (see full diagnostics)
              </div>
            )}
          </div>
        </div>
      )}

      <div className="text-[9px] text-slate-600 italic pt-1">
        Diagnostic only — the gate chain is the source of truth for
        execution. This score summarizes the advisory doctrine layer.
      </div>
    </div>
  );
}

export { computeBreakdown };
