import React, { useMemo } from "react";
import { Card } from "@/components/ui-bits";
import { useMcStream } from "@/hooks/useMcStream";
import { REGIME_META } from "@/components/MarketRegimeTape";

/**
 * Doctrine pin (2026-06-10, P2):
 *
 * The Camaro wrapper raises `CAMARO_WRAPPER_TINY_SCORE_GAP_CHOP_RISK`
 * when the brain ranking gap between top symbols is too thin to act
 * on — i.e., the market is chopping and no setup has real edge.
 * This gauge visualizes both axes:
 *
 *   * Conviction axis  (vertical) — score gap from recent intent activity
 *   * Regime axis      (horizontal) — current classifier verdict
 *
 * The result is a single glance that tells the operator "are the
 * brains finding edge today or just spraying HOLDs in chop?"
 */

function regimeWeight(regime) {
  // Map regime to a chop-likelihood weight in [0, 1].
  // chop/volatile/crisis → high chop risk (right side of the dial)
  // bull/bear           → low chop risk (left side of the dial — clear directional edge)
  // calm                → middle (mixed signals)
  return {
    bull: 0.10,
    bear: 0.10,
    calm: 0.45,
    chop: 0.85,
    volatile: 0.70,
    crisis: 0.95,
  }[regime] ?? 0.50;
}

function holdRatio(intents) {
  if (!intents || intents.length === 0) return 0;
  const holds = intents.filter((i) => (i.action || "").toUpperCase() === "HOLD").length;
  return holds / intents.length;
}

export default function DivergenceChopGauge() {
  const { byType, currentRegime, connected } = useMcStream({ cap: 50 });

  const intents = byType.intent || [];
  const hold = holdRatio(intents);
  const regimeChop = regimeWeight(currentRegime);
  // Composite chop score in [0, 1]. Weight hold-ratio 40%, regime 60%
  // — the regime signal carries more weight because it's derived
  // from the full universe scan, while hold ratio reflects only the
  // intents that happened to land during this connection.
  const chopScore = useMemo(
    () => Math.max(0, Math.min(1, hold * 0.4 + regimeChop * 0.6)),
    [hold, regimeChop],
  );

  // Color the bar by chop score.
  const barColor = chopScore < 0.30 ? "#10B981" : chopScore < 0.60 ? "#F59E0B" : "#EF4444";
  const chopPctLabel = `${Math.round(chopScore * 100)}%`;
  const meta = currentRegime ? REGIME_META[currentRegime] : null;

  // Verdict copy.
  const verdict =
    chopScore < 0.30 ? "edge present"
    : chopScore < 0.60 ? "mixed signal"
    : chopScore < 0.80 ? "chop risk"
    : "deep chop · stay flat";

  return (
    <Card testid="divergence-chop-gauge-card">
      <div className="flex items-center justify-between mb-3">
        <div>
          <div className="text-xs uppercase tracking-wider text-zinc-500">
            Divergence · Chop Gauge
          </div>
          <div className="text-sm text-zinc-400 mt-1">
            Composite of universe regime + recent brain hold-ratio.
            Surfaces `CAMARO_WRAPPER_TINY_SCORE_GAP_CHOP_RISK` conditions before they fire.
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span
            className="h-2 w-2 rounded-full"
            style={{ background: connected ? "#10B981" : "#71717A" }}
            data-testid="chop-gauge-conn-dot"
          />
          <span className="text-xs text-zinc-500">{connected ? "live" : "offline"}</span>
        </div>
      </div>

      <div className="mb-3" data-testid="chop-gauge-bar">
        <div className="flex items-baseline justify-between mb-1">
          <span
            className="text-3xl font-mono font-bold"
            style={{ color: barColor }}
            data-testid="chop-gauge-pct"
          >
            {chopPctLabel}
          </span>
          <span className="text-xs uppercase tracking-wider text-zinc-500">
            {verdict}
          </span>
        </div>
        <div className="h-3 w-full bg-zinc-900 rounded-full overflow-hidden">
          <div
            className="h-full transition-all duration-500"
            style={{
              width: `${chopScore * 100}%`,
              background: `linear-gradient(90deg, #10B98155, ${barColor})`,
            }}
          />
        </div>
        <div className="flex justify-between mt-1 text-[10px] uppercase tracking-wider text-zinc-600">
          <span>directional edge</span>
          <span>deep chop</span>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 text-sm">
        <div className="bg-zinc-900/50 rounded px-3 py-2">
          <div className="text-xs text-zinc-500 uppercase">Regime contribution</div>
          <div className="font-mono text-base mt-1" data-testid="chop-gauge-regime">
            {currentRegime ? `${meta?.label || currentRegime.toUpperCase()} · ${Math.round(regimeChop * 100)}%` : "—"}
          </div>
        </div>
        <div className="bg-zinc-900/50 rounded px-3 py-2">
          <div className="text-xs text-zinc-500 uppercase">HOLD ratio (live)</div>
          <div className="font-mono text-base mt-1" data-testid="chop-gauge-hold-ratio">
            {intents.length === 0
              ? "awaiting intents…"
              : `${Math.round(hold * 100)}% of ${intents.length}`}
          </div>
        </div>
      </div>
    </Card>
  );
}
