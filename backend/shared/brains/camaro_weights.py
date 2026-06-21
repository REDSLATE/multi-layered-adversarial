"""camaro_weights.py — Barracuda wrapper / Camaro decision engine.

Improved from original. Changes documented inline with `# IMPROVED`
comments. 350–420 lines. No DB, no memory surfaces, no telemetry,
no Intent envelope.

Five improvements over the original (operator pin, 2026-02-21):
  1. Dead zone filled — `nano_live (×0.10)` and `seed_live (×0.05)`
     bands added between 0.58–0.65. The original threw away all edge
     in that range.
  2. Graduated loss streak — `×0.85 / ×0.70 / ×0.50 / ×0.25` ramp
     instead of a hard cliff at streak ≥ 4. Brain recalibrates
     progressively now.
  3. Scaled leader penalty — `3_1 → ×0.90`, `2_2 → ×0.82`,
     `no_quorum → ×0.70`. Original applied flat ×0.82 regardless
     of how bad the disagreement was.
  4. Regime-aware RR floor — `1.35` in trending regimes, `1.80` in
     high-vol. Original hardcoded `1.50` everywhere.
  5. `conviction_score` — single 0.0–1.0 composite of signal
     quality, regime confidence, and council quality. Sort
     decisions by this in post-session review.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


# ── ENUMS ───────────────────────────────────────────────────────────


class SizingBand(str, Enum):
    SCALED = "scaled"
    MICRO_LIVE = "micro_live"
    NANO_LIVE = "nano_live"      # IMPROVED: new band, fills the dead zone
    SEED_LIVE = "seed_live"      # IMPROVED: new band, fills the dead zone
    MICRO_PAPER = "micro_paper"
    OBSERVATION = "observation"
    HOLD = "hold"


class EventRisk(str, Enum):
    NORMAL = "normal"
    ELEVATED = "elevated"
    RESTRICTED = "restricted"


class Regime(str, Enum):
    BULL = "bull"
    BEAR = "bear"
    HIGH_VOL = "high_vol"
    NEUTRAL = "neutral"


# ── CONFIDENCE FLOORS ───────────────────────────────────────────────


DIRECTIONAL_COMMITMENT_FLOOR = 0.58
MIN_CONFIDENCE_TO_TRADE = 0.65
RISK_BLOCK_THRESHOLD = 0.65

# IMPROVED: MIN_RR_TO_TRADE is now regime-aware (see
# get_min_rr_for_regime). The old hardcoded 1.50 is now the neutral
# baseline only.
MIN_RR_BASE = 1.50


def get_min_rr_for_regime(regime: Regime) -> float:
    """IMPROVED — Risk/reward floor flexes with market regime.

    High volatility demands more reward for the same risk. Low
    volatility (bull/bear trending) can accept tighter setups.
    """
    return {
        Regime.BULL:     1.35,  # trending, tighter RR acceptable
        Regime.BEAR:     1.35,  # trending short, tighter RR acceptable
        Regime.HIGH_VOL: 1.80,  # wider stops, demand more reward
        Regime.NEUTRAL:  1.50,  # baseline
    }[regime]


# ── SIZING BAND WEIGHTS ─────────────────────────────────────────────
# IMPROVED: Two new bands (nano_live, seed_live) fill the 0.58–0.65
# dead zone. Original: anything below 0.65 sized to zero. Now: small
# live exposure is taken in the 0.58–0.65 range. You learn more from
# small live trades than from pure observation.


SIZING_BAND_WEIGHTS: dict[SizingBand, float] = {
    SizingBand.SCALED:      1.00,   # confidence ≥ 0.80
    SizingBand.MICRO_LIVE:  0.50,   # confidence ≥ 0.72
    SizingBand.MICRO_PAPER: 0.20,   # confidence ≥ 0.65
    SizingBand.NANO_LIVE:   0.10,   # confidence ≥ 0.62   # IMPROVED
    SizingBand.SEED_LIVE:   0.05,   # confidence ≥ 0.58   # IMPROVED
    SizingBand.OBSERVATION: 0.00,   # confidence ≥ 0.50
    SizingBand.HOLD:        0.00,   # confidence < 0.50
}


def resolve_sizing_band(confidence: float) -> tuple[SizingBand, float]:
    """Map confidence to (band, weight)."""
    thresholds = [
        (0.80, SizingBand.SCALED),
        (0.72, SizingBand.MICRO_LIVE),
        (0.65, SizingBand.MICRO_PAPER),
        (0.62, SizingBand.NANO_LIVE),    # IMPROVED
        (0.58, SizingBand.SEED_LIVE),    # IMPROVED
        (0.50, SizingBand.OBSERVATION),
    ]
    for floor, band in thresholds:
        if confidence >= floor:
            return band, SIZING_BAND_WEIGHTS[band]
    return SizingBand.HOLD, 0.00


# ── COUNCIL WEIGHTS ─────────────────────────────────────────────────


REGIME_BULL_BEAR_BOOST = 0.10   # × regime_conf when bull or bear
REGIME_HIGH_VOL_PENALTY = 0.08  # IMPROVED: was 0.05, now symmetric
                                # enough to balance the +0.10
                                # bull/bear boost. Original asymmetry
                                # (+0.10 up / −0.05 down) created a
                                # slight structural long bias.

# IMPROVED: Leader penalty now scales with degree of disagreement.
# Original: flat ×0.82 for any lack of clean agreement.
LEADER_PENALTY_BY_SPLIT: dict[str, float] = {
    "3_1":       0.90,   # mild disagreement
    "2_2":       0.82,   # split council (original value preserved)
    "no_quorum": 0.70,   # no clear leader
}

STRATEGIST_TIEBREAK_EDGE_GAP = 0.35
STRATEGIST_TIEBREAK_MIN_SCORE = 0.60
RISK_PROB_DISCOUNT_WEIGHT = 0.50


def get_leader_penalty(split: str) -> float:
    """IMPROVED — Returns the appropriate multiplier for the council
    split type.

    split: '3_1' | '2_2' | 'no_quorum' | 'clean' (no penalty)
    """
    if split == "clean":
        return 1.00
    return LEADER_PENALTY_BY_SPLIT.get(split, 0.82)


def resolve_council_split(vote_counts: dict[str, int]) -> str:
    """Determine the split label from vote distribution.

    vote_counts: e.g. {"bull": 3, "bear": 1} or {"bull": 2, "bear": 2}
    """
    counts = sorted(vote_counts.values(), reverse=True)
    if not counts:
        return "no_quorum"
    total = sum(counts)
    if counts[0] == total:
        return "clean"
    if total == 4:
        if counts[0] == 3:
            return "3_1"
        if counts[0] == 2:
            return "2_2"
    return "no_quorum"


# ── LOSS STREAK DAMPENER ────────────────────────────────────────────
# IMPROVED: Graduated ramp instead of a hard cliff at streak ≥ 4.
# Original: streak 0–3 = ×1.00, streak ≥ 4 = ×0.50. No gradual
# warning. Now: each additional loss progressively reduces size,
# giving the brain time to recalibrate rather than suddenly halving
# exposure.


LOSS_STREAK_DAMPENERS: list[tuple[int, float]] = [
    (6, 0.25),  # streak ≥ 6 — quarter size
    (4, 0.50),  # streak ≥ 4 — half size (original threshold preserved)
    (3, 0.70),  # streak ≥ 3 — IMPROVED
    (2, 0.85),  # streak ≥ 2 — IMPROVED
    (0, 1.00),  # no streak — full size
]


def get_loss_streak_dampener(streak: int) -> float:
    """IMPROVED — Returns graduated dampener for loss streak length."""
    for threshold, multiplier in LOSS_STREAK_DAMPENERS:
        if streak >= threshold:
            return multiplier
    return 1.00


# ── EVENT RISK DAMPENER ─────────────────────────────────────────────


EVENT_RISK_DAMPENERS: dict[EventRisk, float] = {
    EventRisk.NORMAL:     1.00,
    EventRisk.ELEVATED:   0.75,
    EventRisk.RESTRICTED: 0.00,
}


# ── OUTPUT: WeightedDecision ────────────────────────────────────────


@dataclass
class WeightedDecision:
    # Core decision
    action: str
    direction: str
    confidence: float            # FIX: was missing in PDF source
    raw_confidence: float
    bull_score: float
    bear_score: float
    risk_discount: float
    leader_penalty_applied: bool
    leader_penalty_multiplier: float  # IMPROVED: expose actual multiplier
    council_split: str                # IMPROVED: expose split label
    strategist_tiebreak_applied: bool
    # Sizing
    size_multiplier: float
    band: SizingBand
    band_weight: float
    loss_streak_dampener: float
    event_risk_dampener: float
    # Regime
    regime: Regime
    regime_conf: float
    regime_adjustment: float          # IMPROVED: net regime delta applied
    # Risk
    min_rr_threshold: float           # IMPROVED: expose regime-adjusted RR floor
    vetoes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    # IMPROVED: Single composite conviction score for post-session
    # ranking. Combines bull/bear score quality, regime confidence,
    # and penalties. Range 0.0–1.0. Higher = better quality decision,
    # regardless of outcome.
    conviction_score: float = 0.0


def compute_conviction_score(
    raw_confidence: float,
    regime_conf: float,
    leader_penalty_applied: bool,    # noqa: ARG001 — kept for API parity
    leader_penalty_multiplier: float,
    strategist_tiebreak_applied: bool,
    vetoes: list[str],
) -> float:
    """IMPROVED — Composite conviction quality score.

    Ingredients:
        raw_confidence: base signal quality (weight 0.50)
        regime_conf:    how certain we are of regime (weight 0.25)
        council_quality: penalizes splits and tiebreaks (weight 0.25)

    Not a prediction of outcome — a measure of decision-process
    quality. Use this in post-session review to sort decisions by
    how well-formed they were, independent of whether they won or
    lost.
    """
    # Council quality: starts at 1.0, reduced by penalties.
    council_quality = leader_penalty_multiplier  # already < 1.0 if applied
    if strategist_tiebreak_applied:
        council_quality *= 0.92                  # tiebreak = mild uncertainty
    if vetoes:
        council_quality *= max(0.50, 1.0 - len(vetoes) * 0.10)
    score = (
        raw_confidence * 0.50
        + regime_conf * 0.25
        + council_quality * 0.25
    )
    return round(min(1.0, max(0.0, score)), 4)


# ── MAIN DECISION BUILDER ───────────────────────────────────────────


def build_weighted_decision(
    *,
    action: str,
    direction: str,
    raw_confidence: float,
    bull_score: float,
    bear_score: float,
    risk_prob: float,
    vote_counts: dict[str, int],
    strategist_score: Optional[float],
    edge_gap: float,
    regime: Regime,
    regime_conf: float,
    loss_streak: int,
    event_risk: EventRisk,
    rr_ratio: float,
    vetoes: Optional[list[str]] = None,
    reasons: Optional[list[str]] = None,
) -> WeightedDecision:
    """Assemble a WeightedDecision from raw council inputs.

    Every transformation is explicit and traceable.
    """
    vetoes = list(vetoes or [])
    reasons = list(reasons or [])

    # 1. Regime adjustment — IMPROVED: penalty is now 0.08 (was 0.05)
    #    for better symmetry with the +0.10 boost.
    if regime in (Regime.BULL, Regime.BEAR):
        regime_adjustment = REGIME_BULL_BEAR_BOOST * regime_conf
    elif regime == Regime.HIGH_VOL:
        regime_adjustment = -REGIME_HIGH_VOL_PENALTY   # IMPROVED
    else:
        regime_adjustment = 0.0

    # 2. Risk discount
    risk_discount = RISK_PROB_DISCOUNT_WEIGHT * risk_prob

    # 3. Council split + leader penalty — IMPROVED: graduated penalty
    #    replaces flat ×0.82.
    split = resolve_council_split(vote_counts)
    penalty = get_leader_penalty(split)
    leader_penalty_applied = split != "clean"

    # 4. Strategist tiebreak
    strategist_tiebreak_applied = False
    if (
        edge_gap < STRATEGIST_TIEBREAK_EDGE_GAP
        and strategist_score is not None
        and strategist_score >= STRATEGIST_TIEBREAK_MIN_SCORE
    ):
        strategist_tiebreak_applied = True

    # 5. Adjusted confidence
    adjusted = (raw_confidence + regime_adjustment * risk_discount) * penalty
    confidence = round(min(1.0, max(0.0, adjusted)), 4)

    # 6. Veto checks — IMPROVED: regime-aware RR floor.
    min_rr = get_min_rr_for_regime(regime)
    if confidence < RISK_BLOCK_THRESHOLD:
        vetoes.append("RISK_BLOCK")
    if rr_ratio < min_rr:
        # FIX: PDF source had {{rr_ratio:.2f}} (escaped braces) which
        # would have printed literal '{rr_ratio:.2f}'.
        vetoes.append(f"LOW_RR (need {min_rr}, got {rr_ratio:.2f})")
    if event_risk == EventRisk.RESTRICTED:
        vetoes.append("EVENT_RISK_RESTRICTED")

    # 7. Sizing — IMPROVED: nano_live and seed_live bands fill the
    #    0.58–0.65 dead zone.
    band, band_weight = resolve_sizing_band(confidence)
    streak_dampener = get_loss_streak_dampener(loss_streak)   # IMPROVED
    event_dampener = EVENT_RISK_DAMPENERS[event_risk]
    size_multiplier = round(
        band_weight * streak_dampener * event_dampener, 4,
    )

    # 8. Conviction score — IMPROVED: single composite for
    #    post-session ranking.
    conviction = compute_conviction_score(
        raw_confidence,
        regime_conf,
        leader_penalty_applied,
        penalty,
        strategist_tiebreak_applied,
        vetoes,
    )

    return WeightedDecision(
        action=action,
        direction=direction,
        confidence=confidence,                                # FIX: now exposed
        raw_confidence=raw_confidence,
        bull_score=bull_score,
        bear_score=bear_score,
        risk_discount=risk_discount,
        leader_penalty_applied=leader_penalty_applied,
        leader_penalty_multiplier=penalty,
        council_split=split,                                  # IMPROVED
        strategist_tiebreak_applied=strategist_tiebreak_applied,
        size_multiplier=size_multiplier,
        band=band,
        band_weight=band_weight,
        loss_streak_dampener=streak_dampener,
        event_risk_dampener=event_dampener,
        regime=regime,
        regime_conf=regime_conf,
        regime_adjustment=regime_adjustment,                  # IMPROVED
        min_rr_threshold=min_rr,                              # IMPROVED
        vetoes=vetoes,
        reasons=reasons,
        conviction_score=conviction,                          # IMPROVED
    )


# ── SELF-TEST (mirrors the bullish example from original) ───────────


if __name__ == "__main__":  # pragma: no cover
    d = build_weighted_decision(
        action="BUY",
        direction="bull",
        raw_confidence=0.5133,
        bull_score=0.5133,
        bear_score=0.3200,
        risk_prob=0.20,
        vote_counts={"bull": 3, "bear": 1},
        strategist_score=None,
        edge_gap=0.40,
        regime=Regime.NEUTRAL,
        regime_conf=0.60,
        loss_streak=0,
        event_risk=EventRisk.NORMAL,
        rr_ratio=1.55,
    )
    print(f"action            : {d.action}")
    print(f"confidence        : {d.confidence}")
    print(f"council_split     : {d.council_split}")
    print(
        f"leader_penalty    : {d.leader_penalty_multiplier} "
        f"(applied={d.leader_penalty_applied})"
    )
    print(f"band              : {d.band} weight={d.band_weight}")
    print(f"size_multiplier   : {d.size_multiplier}")
    print(f"min_rr_threshold  : {d.min_rr_threshold}")
    print(f"vetoes            : {d.vetoes}")
    print(f"conviction_score  : {d.conviction_score}")
