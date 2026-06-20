import React from "react";

/**
 * ResearchSignalsBlock — renders the Research Layer chips for one
 * intent.
 *
 * Wired into `IntentPostMortemPanel` (per-intent trace view). Reads
 * `intent.evidence.research_signals` and the status fields the GTO
 * crypto bridge stamps (`research_status`, `research_source`,
 * `research_bars_used`).
 *
 * Doctrine reminder: research is *evidence*, not authority. This
 * panel surfaces what the Strategy Lab saw at emit time; it never
 * implies the brain agreed with the signal. The chip color is
 * decoupled from the brain's final action on purpose.
 *
 * No props beyond `evidence` — the component is read-only and
 * idempotent against missing/empty fields.
 */
const DIRECTION_COLORS = {
  BUY:  { bg: "#064E3B", fg: "#34D399" },   // emerald — long evidence
  SELL: { bg: "#7F1D1D", fg: "#FCA5A5" },   // red — short evidence
  HOLD: { bg: "#1F2937", fg: "#94A3B8" },   // slate — abstain
};

const STATUS_COLORS = {
  ok:                "#10B981",
  no_bars_on_file:   "#94A3B8",
  error:             "#DC2626",
};

export function ResearchSignalsBlock({ evidence }) {
  // No evidence object at all → render nothing. Older intents that
  // pre-date the research wiring should look untouched in the UI.
  if (!evidence || typeof evidence !== "object") return null;
  const status = evidence.research_status;
  const signals = Array.isArray(evidence.research_signals)
    ? evidence.research_signals
    : null;
  if (!status && !signals?.length) return null;

  return (
    <div
      data-testid="research-signals-block"
      className="mt-2 border border-rd-border rounded p-2 text-[10px]"
      style={{ background: "rgba(255,255,255,0.02)" }}
    >
      <div className="flex items-center gap-2 mb-1 text-rd-dim uppercase tracking-wide">
        <span>research evidence</span>
        {status && (
          <span
            data-testid="research-status-pill"
            className="px-1.5 py-0.5 rounded text-[9px]"
            style={{
              background: STATUS_COLORS[status] || "#475569",
              color: "#0B0F19",
              fontWeight: 600,
            }}
          >
            {status}
          </span>
        )}
        {evidence.research_source && (
          <span className="text-rd-dim">src: {evidence.research_source}</span>
        )}
        {typeof evidence.research_bars_used === "number" && (
          <span className="text-rd-dim">bars: {evidence.research_bars_used}</span>
        )}
      </div>

      {/* Error path — render the error string so the operator can act on it
          without opening the intent doc. */}
      {status === "error" && evidence.research_error && (
        <div
          data-testid="research-error-string"
          className="text-rd-dim italic"
        >
          {evidence.research_error}
        </div>
      )}

      {/* Empty case — research ran but lab returned nothing (unknown lane,
          all strategies HOLD with empty reason lists, etc.). */}
      {status === "ok" && signals && signals.length === 0 && (
        <div className="text-rd-dim italic">
          no strategies registered for this lane
        </div>
      )}

      {/* Chips */}
      {signals && signals.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {signals.map((s, i) => {
            const palette = DIRECTION_COLORS[s.direction] || DIRECTION_COLORS.HOLD;
            return (
              <div
                key={`${s.strategy_id || "sig"}-${i}`}
                data-testid={`research-chip-${s.strategy_id || i}`}
                className="rounded px-2 py-1 flex items-center gap-1"
                style={{
                  background: palette.bg,
                  color: palette.fg,
                  border: "1px solid rgba(255,255,255,0.08)",
                }}
                title={
                  `${s.strategy_id} · score=${(s.score ?? 0).toFixed(2)} ` +
                  `· confidence=${(s.confidence ?? 0).toFixed(2)} ` +
                  `· reasons=${(s.reasons || []).join(", ") || "—"}`
                }
              >
                <span className="font-semibold">{s.strategy_id}</span>
                <span>·</span>
                <span>{s.direction}</span>
                <span>·</span>
                <span>score {Number(s.score ?? 0).toFixed(2)}</span>
                {Array.isArray(s.reasons) && s.reasons.length > 0 && (
                  <span className="text-rd-dim hidden sm:inline">
                    [{s.reasons.join(", ")}]
                  </span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default ResearchSignalsBlock;
