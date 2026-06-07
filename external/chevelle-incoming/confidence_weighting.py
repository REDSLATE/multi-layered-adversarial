"""Dynamic confidence weighting and bounded disagreement penalty.

Doctrine (operator note, May 2026):
    Strategist always proposes BUY.
    Council disagrees → confidence collapses to 0.50 → MIN_CONFIDENCE_TO_TRADE
    floor (0.70) is missed → action flattens to HOLD → all brains drift
    toward neutral.

    The fix is NOT to lower the floor. It is to stop collapsing
    disagreement to 0.50.

    Instead:
      - disagreement = bounded UNCERTAINTY PENALTY (not total flattening).
      - per-engine WEIGHTS reflect each voice's recent track record, so
        a reliably-correct strategist still moves the needle even when
        the auditor disagrees, and a chronically-wrong auditor stops
        dragging the council toward neutral.

This module is intentionally pure (no IO, no DB). Inputs come from the
caller; the only side effect is returning a new WeightState. Performance
metrics (winrates, alignment rates) are computed elsewhere — see
`compute_engine_winrates` below for a Mongo-backed helper, but the
formula and the bounded-penalty primitive don't depend on it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Default council-disagreement penalty — multiplicative shave on the
# raw confidence. 0.82 maps a strategist's 0.73 → 0.60, which clears
# the 0.55-0.60 band where the operator wants residual signal to survive,
# while still meaningfully de-rating the conviction. Tune via env.
DEFAULT_COUNCIL_PENALTY = 0.82

# Soft floor — below this, conviction is "I see direction but barely."
# We never collapse to exactly 0.50 in the disagreement path; the floor
# preserves a residual signal the operator can read.
DISAGREEMENT_FLOOR = 0.51


@dataclass
class WeightState:
    strategist_weight: float = 1.0
    auditor_weight: float = 1.0
    commander_weight: float = 1.0
    regime_weight: float = 1.0
    memory_weight: float = 1.0

    def as_dict(self) -> dict[str, float]:
        return {
            "strategist_weight": round(self.strategist_weight, 4),
            "auditor_weight": round(self.auditor_weight, 4),
            "commander_weight": round(self.commander_weight, 4),
            "regime_weight": round(self.regime_weight, 4),
            "memory_weight": round(self.memory_weight, 4),
        }


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def smooth(old: float, target: float, alpha: float = 0.30) -> float:
    """Prevent violent weight swings between recompute cycles."""
    return (old * (1.0 - alpha)) + (target * alpha)


def compute_dynamic_weights(
    *,
    strategist_winrate_20: float,
    auditor_winrate_20: float,
    commander_alignment_rate: float,
    regime_accuracy: float,
    memory_match_winrate: float,
    current: WeightState,
) -> WeightState:
    """Adjust per-engine weights toward target bands based on recent perf.

    Each engine has a neutral target of 1.0. Performance above the upper
    band pushes the target up; below the lower band pushes it down.
    Bands and shifts mirror the operator's spec (May 2026):

      Strategist :  >=0.62 → +0.15  · <=0.45 → -0.20   [clamp 0.50, 1.35]
      Auditor    :  >=0.60 → +0.10  · <=0.45 → -0.15   [clamp 0.50, 1.25]
      Commander  :  >=0.70 → +0.10  · <=0.40 → -0.20   [clamp 0.50, 1.25]
      Regime     :  >=0.60 → +0.10  · <=0.45 → -0.10   [clamp 0.50, 1.20]
      Memory     :  >=0.65 → +0.10  · <=0.40 → -0.15   [clamp 0.50, 1.15]

    Smoothing (alpha=0.30) prevents one bad streak from yanking the
    entire stack — half-life ≈ 2 cycles."""

    strategist_target = 1.0
    auditor_target = 1.0
    commander_target = 1.0
    regime_target = 1.0
    memory_target = 1.0

    if strategist_winrate_20 >= 0.62:
        strategist_target += 0.15
    elif strategist_winrate_20 <= 0.45:
        strategist_target -= 0.20

    if auditor_winrate_20 >= 0.60:
        auditor_target += 0.10
    elif auditor_winrate_20 <= 0.45:
        auditor_target -= 0.15

    if commander_alignment_rate >= 0.70:
        commander_target += 0.10
    elif commander_alignment_rate <= 0.40:
        commander_target -= 0.20

    if regime_accuracy >= 0.60:
        regime_target += 0.10
    elif regime_accuracy <= 0.45:
        regime_target -= 0.10

    if memory_match_winrate >= 0.65:
        memory_target += 0.10
    elif memory_match_winrate <= 0.40:
        memory_target -= 0.15

    return WeightState(
        strategist_weight=clamp(smooth(current.strategist_weight, strategist_target), 0.50, 1.35),
        auditor_weight=clamp(smooth(current.auditor_weight, auditor_target), 0.50, 1.25),
        commander_weight=clamp(smooth(current.commander_weight, commander_target), 0.50, 1.25),
        regime_weight=clamp(smooth(current.regime_weight, regime_target), 0.50, 1.20),
        memory_weight=clamp(smooth(current.memory_weight, memory_target), 0.50, 1.15),
    )


def weighted_confidence(
    *,
    strategist_conf: float,
    auditor_conf: float,
    commander_conf: float,
    regime_conf: float,
    memory_conf: float,
    weights: WeightState,
) -> float:
    """Weighted average of the five voices. Falls back to 0.5 only if
    every weight collapses to zero — which clamp() prevents."""
    num = (
        strategist_conf * weights.strategist_weight
        + auditor_conf * weights.auditor_weight
        + commander_conf * weights.commander_weight
        + regime_conf * weights.regime_weight
        + memory_conf * weights.memory_weight
    )
    den = (
        weights.strategist_weight
        + weights.auditor_weight
        + weights.commander_weight
        + weights.regime_weight
        + weights.memory_weight
    )
    if den <= 0:
        return 0.5
    return clamp(num / den, 0.0, 1.0)


def apply_disagreement_penalty(
    confidence: float,
    *,
    council_disagrees: bool,
    penalty: float = DEFAULT_COUNCIL_PENALTY,
    floor: float = DISAGREEMENT_FLOOR,
) -> tuple[float, float]:
    """Bounded penalty for council disagreement.

    Returns (new_confidence, applied_delta).

    Doctrine: disagreement is UNCERTAINTY, not NEUTRALITY. We multiplicatively
    shave the conviction (default ×0.82) but never collapse it to 0.50 — the
    residual signal is what the operator reads to see "Chevelle still thinks
    BUY, just less certain."

    The floor (default 0.51) is intentionally just above 0.50 so the
    operator can visually distinguish "council-shaved conviction" from a
    genuine HOLD-by-no-signal.
    """
    if not council_disagrees:
        return clamp(confidence, 0.0, 1.0), 0.0
    raw = clamp(confidence, 0.0, 1.0)
    shaved = max(floor, raw * penalty)
    return shaved, shaved - raw


async def compute_engine_winrates(db: Any, lookback: int = 20) -> dict[str, float]:
    """Query the most recent resolved decisions and compute per-voice
    winrates. Used as input to `compute_dynamic_weights`.

    Returns neutral defaults (0.50) when there's insufficient resolved
    history — the user's note flagged this as the current state:
    "No recent resolved outcomes, so Hypothesis recalls neutral history."
    With neutral inputs all weights stay at 1.0 → the weighted formula
    degenerates into a plain average, which is the safe default.

    Schema assumption: `canonical_decisions` documents include
    `outcome.win: bool` once a decision is resolved. If your schema
    stores outcomes elsewhere, adapt the projection — the formula above
    only cares about the five winrates.
    """
    defaults = {
        "strategist_winrate_20": 0.50,
        "auditor_winrate_20": 0.50,
        "commander_alignment_rate": 0.50,
        "regime_accuracy": 0.50,
        "memory_match_winrate": 0.50,
        "resolved_count": 0,
    }
    if db is None:
        return defaults
    try:
        cursor = db.canonical_decisions.find(
            {"outcome": {"$exists": True}, "outcome.win": {"$exists": True}},
            {
                "_id": 0,
                "outcome": 1,
                "raw_prediction.signal": 1,
                "raw_prediction.adversarial.binding_voice": 1,
                "context_snapshot_id": 1,
            },
        ).sort("created_at", -1).limit(lookback)
        docs = await cursor.to_list(length=lookback)
    except Exception:                                       # noqa: BLE001
        return defaults

    if not docs:
        return defaults

    total = len(docs)
    wins = sum(1 for d in docs if (d.get("outcome") or {}).get("win"))
    base_rate = wins / total

    # Without per-voice attribution in the schema yet, all five voices
    # share the base rate. The user's spec acknowledged this; per-voice
    # winrate columns can be backfilled later.
    return {
        "strategist_winrate_20": round(base_rate, 4),
        "auditor_winrate_20": round(base_rate, 4),
        "commander_alignment_rate": round(base_rate, 4),
        "regime_accuracy": round(base_rate, 4),
        "memory_match_winrate": round(base_rate, 4),
        "resolved_count": total,
    }


__all__ = [
    "DEFAULT_COUNCIL_PENALTY",
    "DISAGREEMENT_FLOOR",
    "WeightState",
    "apply_disagreement_penalty",
    "clamp",
    "compute_dynamic_weights",
    "compute_engine_winrates",
    "smooth",
    "weighted_confidence",
]
