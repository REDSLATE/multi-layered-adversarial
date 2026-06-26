"""Consensus engine — `build_consensus(opinions, regime) -> intent`.

Mirrors the operator's 2026-02-23 spec verbatim. Pure function — no
I/O, no DB, no env reads, no side effects. The caller supplies the
opinions and the current regime; this returns one `ConsensusIntent`
(or None if there are no opinions to synthesize).

Doctrine pin: this is the synthesis layer Paradox runs BEFORE the
seat authorizes. The seat may then weigh the consensus (operator's
NVDA worked-example) and emit ONE decision. The consensus engine
itself does NOT decide who executes; it just produces the
synthesized opinion the seat acts on.
"""
from __future__ import annotations

from collections import defaultdict

from shared.consensus import BrainOpinion, ConsensusIntent


# Equal default weights — no brain is "more right" until calibration
# kicks in. The kernel/grading layer will adjust these later based on
# observed outcomes.
BASE_WEIGHTS: dict[str, float] = {
    "camino":    1.00,
    "barracuda": 1.00,
    "hellcat":   1.00,
    "gto":       1.00,
}


# Regime-conditioned weights — each brain's strength surfaces in the
# regime that matches its doctrine. Doctrinal pin per brain:
#     camino    -> trend doctrine
#     barracuda -> mean reversion (range)
#     hellcat   -> breakout
#     gto       -> momentum (adversarial short hunter — narrower BUY,
#                  but still meaningful weight in trends/breakouts)
REGIME_WEIGHTS: dict[str, dict[str, float]] = {
    "trend": {
        "camino":    1.35,
        "gto":       1.20,
        "hellcat":   0.90,
        "barracuda": 0.70,
    },
    "range": {
        "barracuda": 1.35,
        "camino":    0.85,
        "gto":       0.90,
        "hellcat":   0.80,
    },
    "breakout": {
        "hellcat":   1.40,
        "gto":       1.15,
        "camino":    1.00,
        "barracuda": 0.65,
    },
    "neutral": {
        "camino":    1.00,
        "barracuda": 1.00,
        "hellcat":   1.00,
        "gto":       1.00,
    },
}


# Map the broader market_regime vocabulary used elsewhere in the
# codebase (bull/bear/chop/sideways/risk_on/risk_off/calm_bull/strong)
# onto the four engine-recognized regimes. Anything not in the map
# falls back to "neutral".
_REGIME_ALIASES: dict[str, str] = {
    "trend":      "trend",
    "bull":       "trend",
    "bear":       "trend",
    "risk_on":    "trend",
    "calm_bull":  "trend",
    "strong":     "trend",
    "range":      "range",
    "chop":       "range",
    "sideways":   "range",
    "breakout":   "breakout",
    "neutral":    "neutral",
    "unknown":    "neutral",
    "risk_off":   "neutral",
    "crisis":     "neutral",
}


def normalize_regime(regime: str | None) -> str:
    if not regime:
        return "neutral"
    return _REGIME_ALIASES.get(regime.strip().lower(), "neutral")


def build_consensus(
    opinions: list[BrainOpinion],
    market_regime: str = "neutral",
    min_confidence: float = 0.45,
    min_margin: float = 0.05,
) -> ConsensusIntent | None:
    """Synthesize a `ConsensusIntent` from a list of `BrainOpinion`s.

    Args:
        opinions: every opinion to weight. Must all be for the same
            (symbol, lane); the caller is responsible for grouping.
        market_regime: one of trend / range / breakout / neutral
            (or any alias mapped by `normalize_regime`).
        min_confidence: if the top action's share of total weighted
            score is below this floor, return HOLD (consensus weak).
        min_margin: if the gap between the top action's share and
            the second-place share is below this, return HOLD
            (consensus split — abstain rather than guess).

    Returns:
        `ConsensusIntent` with action=BUY/SELL/HOLD/SHORT/COVER, or
        None if there are no opinions to synthesize at all.
    """
    if not opinions:
        return None

    symbol = opinions[0].symbol
    lane = opinions[0].lane
    regime = normalize_regime(market_regime)

    scores: dict[str, float] = defaultdict(float)
    brain_votes: dict[str, list[str]] = defaultdict(list)
    regime_weights = REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS["neutral"])

    for op in opinions:
        if op.action == "ABSTAIN":
            continue
        base_weight = BASE_WEIGHTS.get(op.brain, 1.0)
        regime_weight = regime_weights.get(op.brain, 1.0)
        weight = base_weight * regime_weight
        score = float(op.confidence or 0.0) * weight
        scores[op.action] += score
        brain_votes[op.action].append(op.brain)

    if not scores:
        return None

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_action, top_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0

    total = sum(scores.values())
    consensus_conf = top_score / total if total else 0.0
    margin = consensus_conf - (
        (second_score / total) if total else 0.0
    )

    # Down-grade to HOLD when the consensus is weak or split. The seat
    # holder still sees the breakdown via `evidence` so it can
    # override if it has reason to.
    final_action: str = top_action
    if consensus_conf < min_confidence:
        final_action = "HOLD"
    if margin < min_margin:
        final_action = "HOLD"

    agreed = list(brain_votes.get(final_action, []))
    if final_action == "HOLD":
        # No HOLD agreement set when the engine itself forced HOLD —
        # the seat sees the original split as the disagreement record.
        agreed = []
    disagreed = sorted({
        op.brain for op in opinions
        if op.action != final_action and op.action != "ABSTAIN"
    } - set(agreed))

    return ConsensusIntent(
        symbol=symbol,
        lane=lane,
        action=final_action,  # type: ignore[arg-type]
        confidence=round(float(consensus_conf), 4),
        agreed_brains=agreed,
        disagreed_brains=disagreed,
        evidence={
            "market_regime": regime,
            "market_regime_raw": market_regime,
            "raw_scores": {k: round(float(v), 4) for k, v in scores.items()},
            "margin": round(float(margin), 4),
            "opinion_count": len(opinions),
            "min_confidence_floor": min_confidence,
            "min_margin_floor": min_margin,
            "engine_forced_hold": final_action == "HOLD" and top_action != "HOLD",
            "top_action_pre_floor": top_action,
        },
    )


__all__ = [
    "BASE_WEIGHTS", "REGIME_WEIGHTS",
    "build_consensus", "normalize_regime",
]
