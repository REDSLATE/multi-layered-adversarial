"""Legacy brain wrappers — Alpha executor & Chevelle governor.

Doctrine pin (operator directive, 2026-06-XX):

    new brain engine
        + old Alpha executor instincts
        + old Chevelle governor instincts
    without locking either one into a seat

This module sits AFTER the new doctrine-driven brain evaluates and
BEFORE the intent is posted to MC. Each wrapper:

    * adjusts confidence within bounds [0.0, 1.0]
    * scales a `size_bias` multiplier within [0.0, 2.0]
    * appends `reasons` and `warnings`
    * stamps an `evidence.legacy_wrapper` provenance block

A wrapper NEVER:

    * creates a trade from HOLD
    * flips BUY ↔ SELL
    * forces a seat assignment
    * changes the brain's doctrine

Assignment (operator-pinned):

    Camino    → alpha_legacy_executor       (executor discipline)
    Hellcat   → chevelle_legacy_governor    (risk compression)
    Barracuda → camaro_legacy_strategist    (tape-reading)
    GTO       → redeye_legacy_adversary     (adversary / opponent)

Final matrix:
    Camino    = trend          + Alpha executor discipline
    Barracuda = mean reversion + Camaro tape reading
    Hellcat   = breakout       + Chevelle risk compression
    GTO       = momentum       + RedEye adversary / opponent

The brain still emits its own doctrine-driven hypothesis; the
wrapper layers in the old-personality instincts on top. Same brain
in a different seat tomorrow still carries the same wrapper.

────────────────────────────────────────────────────────────────────
2026-02-19 — Penalty-stacking dampener (operator directive)

The four wrappers above multiply `size_bias` 6-9 times each. A real-
world BUY on AAPL in a chop regime with unknown position state could
hit 4-6 penalty factors compounding: 0.80 × 0.75 × 0.70 × 0.85 × 0.50
= ~0.18. The wrapper says "BUY size 0.18×" — by the time the ladder
sizer is done with it, the intent is functionally a shadow ping with
no real exposure. The brain "wanted" to buy; the wrapper silenced it.

Two env knobs let the operator dial this in without code changes:

  RISEDUAL_WRAPPER_PENALTY_STRENGTH   default 1.0
        Global multiplier on the DEVIATION from each wrapper's base
        input. 1.0 keeps current behavior (full penalty stacking).
        0.5 cuts every penalty in half. 0.0 nullifies the wrapper
        entirely (size_bias and confidence pass through unchanged).
        Applies to BOTH penalties (factor < 1) and bonuses (factor >
        1) symmetrically — softening the wrapper's voice, not its
        bias direction.

  RISEDUAL_WRAPPER_MIN_SIZE_BIAS_NONZERO   default 0.0
        Floor for `size_bias` on DIRECTIONAL intents (BUY/SELL only).
        If the stacked penalties drag size_bias below this floor,
        clamp UP to the floor. HOLD intents are unaffected — they
        always size to 0.0 (the HOLD-zeroing branches are preserved).
        Doctrine: a directional intent the brain is willing to publish
        deserves a minimum executable footprint; below that floor it
        belongs in HOLD, not in micro-shadow purgatory.

The dampener is applied via `_finalise_size_and_confidence` at the
TAIL of each wrapper — wrapper internals are unchanged, only the
final clamp/floor step is new. This keeps the multiplicative penalty
logic intact for diagnostic purposes (warnings still tell the
operator WHY size was compressed) but lets the operator decide how
HARD to listen to that compression.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from typing import Any, Literal


# ── Penalty-stacking dampener (2026-02-19 operator directive) ──
# Defaults preserve current behavior. Operator dials these via env
# without restarting the runner (re-read each call via os.environ
# lookup → no caching). The cost of an env read per intent is
# negligible at the brain's tick cadence (4 brains × <1Hz emit).
#
# 2026-02-19 (rev2 — prod size-bias crush): floor lifted from 0.0
# → 0.3 after operator observed EVERY directional intent on prod
# arriving at the gate chain with `size_bias=0.000`. The wrapper
# chain (camaro wrapper × chevelle governor × risk transitions)
# was compressing the 0.8 base into 0 within 2-3 multiplicative
# hops, putting effective_notional below the $1 cap floor and
# blocking submit. 0.3 floor = "a directional intent the brain
# is willing to publish deserves a minimum executable footprint;
# below that floor it belongs in HOLD, not in micro-shadow
# purgatory." Strength stays 1.0 — full penalties still fire,
# they just can't crush below the floor.
_WRAPPER_PENALTY_STRENGTH_DEFAULT = 1.0
_WRAPPER_MIN_SIZE_BIAS_NONZERO_DEFAULT = 0.3


def _wrapper_penalty_strength() -> float:
    """Read the global penalty-stacking dampener from env.

    Returns a float in [0.0, 1.0]. Out-of-range values are clamped
    rather than rejected — fail-soft, since the wrapper layer is on
    the hot path and the operator may typo a value during a live
    tune session.
    """
    raw = os.environ.get(
        "RISEDUAL_WRAPPER_PENALTY_STRENGTH",
        str(_WRAPPER_PENALTY_STRENGTH_DEFAULT),
    )
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return _WRAPPER_PENALTY_STRENGTH_DEFAULT
    return max(0.0, min(1.0, v))


def _wrapper_min_size_bias_nonzero() -> float:
    """Read the directional size-bias floor from env. Clamped to
    [0.0, 1.0] — the floor is a directional minimum, not a multiplier."""
    raw = os.environ.get(
        "RISEDUAL_WRAPPER_MIN_SIZE_BIAS_NONZERO",
        str(_WRAPPER_MIN_SIZE_BIAS_NONZERO_DEFAULT),
    )
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return _WRAPPER_MIN_SIZE_BIAS_NONZERO_DEFAULT
    return max(0.0, min(1.0, v))


def _finalise_size_and_confidence(
    final_size_bias: float,
    final_confidence: float,
    base_size_bias: float,
    base_confidence: float,
    action: str,
) -> tuple[float, float, dict[str, Any]]:
    """Apply the env-controlled penalty dampener + directional floor.

    Args:
        final_size_bias: the wrapper's accumulated size_bias after
            all its `*=` multipliers fired.
        final_confidence: the wrapper's accumulated confidence after
            all its `+=`/`-=` adjustments.
        base_size_bias: the size_bias the wrapper STARTED with
            (the intent's input). Used to compute the deviation the
            wrapper applied.
        base_confidence: same for confidence.
        action: "BUY" / "SELL" / "HOLD". Directional floor only
            applies to BUY/SELL; HOLD stays at 0.

    Returns:
        (size_bias, confidence, dampener_diagnostics) — the dampener
        diagnostics dict gets stamped into evidence.legacy_wrapper
        so the operator can inspect what was applied.
    """
    strength = _wrapper_penalty_strength()
    floor = _wrapper_min_size_bias_nonzero()

    pre_dampener_size_bias = final_size_bias
    pre_dampener_confidence = final_confidence

    # Soften the deviation from base by `strength`. At strength=1.0
    # the wrapper's full output passes through (current behavior).
    # At strength=0.0 the wrapper's deviation is zeroed out — the
    # intent reverts to its base values (de-wrappered).
    if strength != 1.0:
        final_size_bias = (
            base_size_bias + (final_size_bias - base_size_bias) * strength
        )
        final_confidence = (
            base_confidence + (final_confidence - base_confidence) * strength
        )

    # Directional floor — keep BUY/SELL above the operator's minimum
    # so a stack of penalties can't reduce a real intent to a micro-
    # shadow. HOLD intents are intentionally NOT floored (the wrappers
    # explicitly zero them inside; we preserve that contract).
    floored_from: float | None = None
    if action in ("BUY", "SELL") and 0.0 < final_size_bias < floor:
        floored_from = final_size_bias
        final_size_bias = floor

    # Final clamp to the doctrine bounds.
    final_size_bias = clamp(final_size_bias, 0.0, 2.0)
    final_confidence = clamp(final_confidence, 0.0, 1.0)

    diagnostics: dict[str, Any] = {
        "penalty_strength": strength,
        "min_size_bias_nonzero": floor,
    }
    # Only stamp diagnostics when the dampener actually changed
    # something — keeps the evidence blob clean in the default-
    # configured case.
    if strength != 1.0:
        diagnostics["pre_dampener_size_bias"] = round(pre_dampener_size_bias, 4)
        diagnostics["pre_dampener_confidence"] = round(pre_dampener_confidence, 4)
    if floored_from is not None:
        diagnostics["floored_size_bias_from"] = round(floored_from, 4)
    return final_size_bias, final_confidence, diagnostics


WrapperName = Literal[
    "alpha_legacy_executor",
    "chevelle_legacy_governor",
    "camaro_legacy_strategist",
    "redeye_legacy_adversary",
]


@dataclass(frozen=True)
class WrappedIntent:
    brain_id: str
    display_name: str
    wrapper: str
    parent_brain: str
    doctrine: str

    action: str
    confidence: float
    size_bias: float

    current_side: str | None
    transition_intent: str | None
    position_evolution: str | None
    risk_transition: str | None

    reasons: list[str]
    warnings: list[str]
    evidence: dict[str, Any]


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _base_fields(intent: dict[str, Any]) -> dict[str, Any]:
    return {
        "brain_id": intent.get("brain_id", "unknown"),
        "display_name": intent.get("display_name", intent.get("brain_id", "unknown")),
        "action": intent.get("action", "HOLD"),
        "confidence": safe_float(intent.get("confidence"), 0.0),
        "size_bias": safe_float(intent.get("size_bias"), 1.0),
        "current_side": intent.get("current_side"),
        "transition_intent": intent.get("transition_intent"),
        "position_evolution": intent.get("position_evolution"),
        "risk_transition": intent.get("risk_transition"),
        "reasons": list(intent.get("reasons", []) or []),
        "warnings": list(intent.get("warnings", []) or []),
        "evidence": dict(intent.get("evidence", {}) or {}),
    }


def apply_alpha_legacy_executor(intent: dict[str, Any]) -> dict[str, Any]:
    """
    Alpha wrapper.

    Purpose:
    - executor discipline
    - stronger commitment when position-transition is clean
    - reduce confidence when position state is unknown
    - reward OPEN / ADD / SCALE_IN only when confidence already supports it

    Does NOT:
    - create trades from HOLD
    - flip BUY/SELL
    - force a seat
    """

    x = _base_fields(intent)

    confidence = x["confidence"]
    size_bias = x["size_bias"]
    reasons = x["reasons"]
    warnings = x["warnings"]
    evidence = x["evidence"]

    action = x["action"]
    current_side = x["current_side"]
    transition = x["transition_intent"]
    evolution = x["position_evolution"]

    if current_side in {None, "UNKNOWN"}:
        confidence -= 0.08
        size_bias *= 0.70
        warnings.append("ALPHA_WRAPPER_POSITION_STATE_UNKNOWN")

    if action in {"BUY", "SELL"} and transition in {
        "OPEN_LONG",
        "OPEN_SHORT",
        "ADD_LONG",
        "ADD_SHORT",
    }:
        if confidence >= 0.68:
            confidence += 0.04
            size_bias *= 1.05
            reasons.append("ALPHA_WRAPPER_CLEAN_EXECUTION_COMMITMENT")
        else:
            confidence -= 0.03
            size_bias *= 0.85
            warnings.append("ALPHA_WRAPPER_WEAK_COMMITMENT_FOR_EXPOSURE_INCREASE")

    if evolution in {"SCALE_IN"}:
        if confidence >= 0.72:
            confidence += 0.03
            size_bias *= 1.10
            reasons.append("ALPHA_WRAPPER_SCALE_IN_CONFIRMED")
        else:
            confidence -= 0.05
            size_bias *= 0.75
            warnings.append("ALPHA_WRAPPER_SCALE_IN_NOT_CONFIRMED")

    if evolution in {"SCALE_OUT", "PARTIAL_COVER", "FULL_COVER"}:
        confidence += 0.02
        size_bias *= 0.95
        reasons.append("ALPHA_WRAPPER_POSITION_MANAGEMENT_OK")

    if transition in {"FLIP_LONG_TO_SHORT", "FLIP_SHORT_TO_LONG", "FLIP"}:
        confidence -= 0.10
        size_bias *= 0.50
        warnings.append("ALPHA_WRAPPER_FLIP_REQUIRES_STRONG_CONFIRMATION")

    evidence["legacy_wrapper"] = {
        "name": "alpha_legacy_executor",
        "parent_brain": "alpha",
        "effect": "executor_commitment_and_position_discipline",
    }

    # ── Penalty-stacking dampener (2026-02-19 operator directive) ──
    size_bias, confidence, _damp = _finalise_size_and_confidence(
        final_size_bias=size_bias,
        final_confidence=confidence,
        base_size_bias=x["size_bias"],
        base_confidence=x["confidence"],
        action=action,
    )
    evidence["legacy_wrapper"]["dampener"] = _damp

    wrapped = WrappedIntent(
        brain_id=x["brain_id"],
        display_name=x["display_name"],
        wrapper="alpha_legacy_executor",
        parent_brain="alpha",
        doctrine="executor_confirming",
        action=action,
        confidence=round(confidence, 4),
        size_bias=round(size_bias, 4),
        current_side=current_side,
        transition_intent=transition,
        position_evolution=evolution,
        risk_transition=x["risk_transition"],
        reasons=list(dict.fromkeys(reasons)),
        warnings=list(dict.fromkeys(warnings)),
        evidence=evidence,
    )

    return asdict(wrapped)


def apply_chevelle_legacy_governor(intent: dict[str, Any]) -> dict[str, Any]:
    """
    Chevelle wrapper.

    Purpose:
    - governor/risk temperament
    - compresses size before blocking
    - penalizes risky transitions in stressed regimes
    - rewards reductions/covers during RISK_OFF

    Does NOT:
    - create trades from HOLD
    - flip BUY/SELL
    - force a seat
    """

    x = _base_fields(intent)

    confidence = x["confidence"]
    size_bias = x["size_bias"]
    reasons = x["reasons"]
    warnings = x["warnings"]
    evidence = x["evidence"]

    action = x["action"]
    current_side = x["current_side"]
    transition = x["transition_intent"]
    evolution = x["position_evolution"]
    risk_transition = x["risk_transition"]

    if risk_transition == "RISK_OFF":
        if evolution in {"SCALE_OUT", "PARTIAL_COVER", "FULL_COVER"}:
            confidence += 0.04
            size_bias *= 1.00
            reasons.append("CHEVELLE_WRAPPER_RISK_OFF_REDUCTION_APPROVED")
        elif transition in {"OPEN_LONG", "OPEN_SHORT", "ADD_LONG", "ADD_SHORT"}:
            confidence -= 0.12
            size_bias *= 0.50
            warnings.append("CHEVELLE_WRAPPER_RISK_OFF_EXPOSURE_INCREASE_COMPRESSED")

    if risk_transition == "RISK_ON":
        if transition in {"OPEN_LONG", "ADD_LONG", "OPEN_SHORT", "ADD_SHORT"}:
            confidence += 0.02
            size_bias *= 1.00
            reasons.append("CHEVELLE_WRAPPER_RISK_ON_EXPOSURE_ALLOWED")

    if evolution == "SCALE_IN":
        size_bias *= 0.80
        warnings.append("CHEVELLE_WRAPPER_SCALE_IN_SIZE_COMPRESSION")

    if transition in {"FLIP_LONG_TO_SHORT", "FLIP_SHORT_TO_LONG", "FLIP"}:
        confidence -= 0.15
        size_bias *= 0.35
        warnings.append("CHEVELLE_WRAPPER_FLIP_HEAVILY_COMPRESSED")

    if current_side in {None, "UNKNOWN"}:
        confidence -= 0.10
        size_bias *= 0.60
        warnings.append("CHEVELLE_WRAPPER_POSITION_STATE_UNKNOWN")

    if action == "HOLD":
        size_bias = 0.0

    evidence["legacy_wrapper"] = {
        "name": "chevelle_legacy_governor",
        "parent_brain": "chevelle",
        "effect": "risk_compression_and_governor_temperament",
    }

    # ── Penalty-stacking dampener (2026-02-19 operator directive) ──
    size_bias, confidence, _damp = _finalise_size_and_confidence(
        final_size_bias=size_bias,
        final_confidence=confidence,
        base_size_bias=x["size_bias"],
        base_confidence=x["confidence"],
        action=action,
    )
    evidence["legacy_wrapper"]["dampener"] = _damp

    wrapped = WrappedIntent(
        brain_id=x["brain_id"],
        display_name=x["display_name"],
        wrapper="chevelle_legacy_governor",
        parent_brain="chevelle",
        doctrine="adaptive_governor",
        action=action,
        confidence=round(confidence, 4),
        size_bias=round(size_bias, 4),
        current_side=current_side,
        transition_intent=transition,
        position_evolution=evolution,
        risk_transition=risk_transition,
        reasons=list(dict.fromkeys(reasons)),
        warnings=list(dict.fromkeys(warnings)),
        evidence=evidence,
    )

    return asdict(wrapped)

def apply_camaro_legacy_strategist(intent: dict[str, Any]) -> dict[str, Any]:
    """
    Camaro wrapper.

    Purpose:
    - live-market strategist temperament
    - rewards clean directional tape
    - avoids tiny BUY/SELL gaps
    - favors continuation with position-aware transitions
    - compresses size in chop / unclear regime

    Does NOT:
    - create trades from HOLD
    - flip BUY/SELL
    - force a seat
    """

    x = _base_fields(intent)

    confidence = x["confidence"]
    size_bias = x["size_bias"]
    reasons = x["reasons"]
    warnings = x["warnings"]
    evidence = x["evidence"]

    action = x["action"]
    current_side = x["current_side"]
    transition = x["transition_intent"]
    evolution = x["position_evolution"]
    risk_transition = x["risk_transition"]

    # Optional evidence fields if your intent carries them
    market_regime = evidence.get("market_regime") or evidence.get("snapshot", {}).get("market_regime")
    buy_score = safe_float(evidence.get("buy_score"), 0.0)
    sell_score = safe_float(evidence.get("sell_score"), 0.0)
    score_gap = abs(buy_score - sell_score)

    # Camaro hates indecision/chop.
    if score_gap and score_gap < 0.035:
        confidence -= 0.06
        size_bias *= 0.75
        warnings.append("CAMARO_WRAPPER_TINY_SCORE_GAP_CHOP_RISK")

    # Live-market continuation bias.
    if market_regime in {"bull", "risk_on", "calm_bull"}:
        if action == "BUY" and transition in {"OPEN_LONG", "ADD_LONG"}:
            confidence += 0.04
            size_bias *= 1.05
            reasons.append("CAMARO_WRAPPER_BULL_REGIME_LONG_CONTINUATION")
        elif action == "SELL" and transition in {"OPEN_SHORT", "ADD_SHORT"}:
            confidence -= 0.05
            size_bias *= 0.80
            warnings.append("CAMARO_WRAPPER_SHORT_AGAINST_BULL_REGIME")

    if market_regime in {"bear", "risk_off", "crisis"}:
        if action == "SELL" and transition in {"OPEN_SHORT", "ADD_SHORT"}:
            confidence += 0.04
            size_bias *= 1.05
            reasons.append("CAMARO_WRAPPER_BEAR_REGIME_SHORT_CONTINUATION")
        elif action == "BUY" and transition in {"OPEN_LONG", "ADD_LONG"}:
            confidence -= 0.05
            size_bias *= 0.80
            warnings.append("CAMARO_WRAPPER_LONG_AGAINST_BEAR_REGIME")

    # In chop, reduce appetite.
    if market_regime in {"chop", "sideways", "unknown", None}:
        if transition in {"OPEN_LONG", "OPEN_SHORT", "ADD_LONG", "ADD_SHORT"}:
            confidence -= 0.04
            size_bias *= 0.80
            warnings.append("CAMARO_WRAPPER_CHOP_EXPOSURE_COMPRESSION")

    # Camaro likes position management more than blind reversal.
    if evolution in {"SCALE_OUT", "PARTIAL_COVER", "FULL_COVER"}:
        confidence += 0.02
        reasons.append("CAMARO_WRAPPER_POSITION_MANAGEMENT_APPROVED")

    # Camaro is cautious on flips unless confidence is already high.
    if transition in {"FLIP_LONG_TO_SHORT", "FLIP_SHORT_TO_LONG", "FLIP"}:
        if confidence >= 0.76:
            confidence += 0.01
            size_bias *= 0.75
            warnings.append("CAMARO_WRAPPER_HIGH_CONFIDENCE_FLIP_COMPRESSED")
        else:
            confidence -= 0.12
            size_bias *= 0.45
            warnings.append("CAMARO_WRAPPER_LOW_CONFIDENCE_FLIP_REJECTED_BY_TEMPERAMENT")

    # If current side is known and action aligns with existing exposure, slightly reward continuation.
    if current_side == "LONG" and transition == "ADD_LONG" and confidence >= 0.68:
        confidence += 0.03
        reasons.append("CAMARO_WRAPPER_LONG_CONTINUATION_CONFIRMED")

    if current_side == "SHORT" and transition == "ADD_SHORT" and confidence >= 0.68:
        confidence += 0.03
        reasons.append("CAMARO_WRAPPER_SHORT_CONTINUATION_CONFIRMED")

    if action == "HOLD":
        size_bias = 0.0

    # Squeeze Detector V2 (operator-shipped 2026-06-11). Barracuda is
    # the tape strategist — when the squeeze grade is A and the brain
    # already wants long, the squeeze confirms the tape; when the
    # grade is F (data error / stale), Barracuda steps back. When
    # `already_fading_from_high` fires, Barracuda compresses size hard
    # (it's the "don't chase tops" rule baked into the wrapper).
    snap = evidence.get("snapshot") or {}
    sq = snap.get("squeeze") or {}
    sq_grade = str(sq.get("grade") or "").upper()
    sq_risks = sq.get("risk_flags") or []
    if sq_grade == "A" and action == "BUY":
        confidence += 0.06
        size_bias *= 1.10
        reasons.append("CAMARO_WRAPPER_SQUEEZE_A_TAPE_CONFIRMED")
    elif sq_grade == "B" and action == "BUY":
        confidence += 0.02
        reasons.append("CAMARO_WRAPPER_SQUEEZE_B_TAPE_OK")
    elif sq_grade == "F":
        confidence -= 0.08
        size_bias *= 0.60
        warnings.append("CAMARO_WRAPPER_SQUEEZE_DATA_ERROR_OR_STALE")
    if "already_fading_from_high" in sq_risks and action == "BUY":
        confidence -= 0.10
        size_bias *= 0.55
        warnings.append("CAMARO_WRAPPER_SQUEEZE_FADING_FROM_HIGH_NO_CHASE")
    if "wide_spread_risk" in sq_risks:
        size_bias *= 0.75
        warnings.append("CAMARO_WRAPPER_SQUEEZE_WIDE_SPREAD_COMPRESSED")

    evidence["legacy_wrapper"] = {
        "name": "camaro_legacy_strategist",
        "parent_brain": "camaro",
        "effect": "live_market_tape_reading_and_continuation_bias",
    }

    # ── Penalty-stacking dampener (2026-02-19 operator directive) ──
    size_bias, confidence, _damp = _finalise_size_and_confidence(
        final_size_bias=size_bias,
        final_confidence=confidence,
        base_size_bias=x["size_bias"],
        base_confidence=x["confidence"],
        action=action,
    )
    evidence["legacy_wrapper"]["dampener"] = _damp

    wrapped = WrappedIntent(
        brain_id=x["brain_id"],
        display_name=x["display_name"],
        wrapper="camaro_legacy_strategist",
        parent_brain="camaro",
        doctrine="live_market_strategist",
        action=action,
        confidence=round(confidence, 4),
        size_bias=round(size_bias, 4),
        current_side=current_side,
        transition_intent=transition,
        position_evolution=evolution,
        risk_transition=risk_transition,
        reasons=list(dict.fromkeys(reasons)),
        warnings=list(dict.fromkeys(warnings)),
        evidence=evidence,
    )

    return asdict(wrapped)




def apply_redeye_legacy_adversary(intent: dict[str, Any]) -> dict[str, Any]:
    """
    RedEye wrapper.

    Purpose:
    - adversarial/opponent temperament
    - challenges weak consensus
    - rewards downside / short continuation when tape supports it
    - penalizes crowded or low-gap longs
    - keeps GTO as the pure contrarian pressure source

    Does NOT:
    - create trades from HOLD
    - flip BUY/SELL
    - force a seat
    """

    x = _base_fields(intent)

    confidence = x["confidence"]
    size_bias = x["size_bias"]
    reasons = x["reasons"]
    warnings = x["warnings"]
    evidence = x["evidence"]

    action = x["action"]
    current_side = x["current_side"]
    transition = x["transition_intent"]
    evolution = x["position_evolution"]
    risk_transition = x["risk_transition"]

    market_regime = evidence.get("market_regime") or evidence.get("snapshot", {}).get("market_regime")
    buy_score = safe_float(evidence.get("buy_score"), 0.0)
    sell_score = safe_float(evidence.get("sell_score"), 0.0)
    score_gap = abs(buy_score - sell_score)

    flow_imbalance = safe_float(evidence.get("flow_imbalance"), 0.0)
    liquidity_stress = safe_float(evidence.get("liquidity_stress"), 0.0)  # noqa: F841  # reserved for future stress-band rule
    news_zscore = safe_float(evidence.get("news_zscore"), 0.0)

    # RedEye distrusts weak consensus.
    if score_gap and score_gap < 0.04:
        confidence -= 0.05
        size_bias *= 0.70
        warnings.append("REDEYE_WRAPPER_WEAK_CONSENSUS_CHALLENGED")

    # RedEye is especially suspicious of longs during stress/risk-off.
    if action == "BUY" and risk_transition == "RISK_OFF":
        confidence -= 0.12
        size_bias *= 0.45
        warnings.append("REDEYE_WRAPPER_LONG_AGAINST_RISK_OFF_COMPRESSED")

    # RedEye rewards short pressure in risk-off or bear regimes.
    if action == "SELL" and transition in {"OPEN_SHORT", "ADD_SHORT"}:
        if risk_transition == "RISK_OFF" or market_regime in {"bear", "risk_off", "crisis"}:
            confidence += 0.06
            size_bias *= 1.08
            reasons.append("REDEYE_WRAPPER_SHORT_PRESSURE_CONFIRMED")

    # If already short, RedEye likes continuation only when confidence is real.
    if current_side == "SHORT" and transition == "ADD_SHORT":
        if confidence >= 0.66:
            confidence += 0.04
            size_bias *= 1.05
            reasons.append("REDEYE_WRAPPER_SHORT_CONTINUATION")
        else:
            confidence -= 0.04
            size_bias *= 0.75
            warnings.append("REDEYE_WRAPPER_WEAK_SHORT_ADD_COMPRESSED")

    # RedEye warns when covering too early during downside pressure.
    if current_side == "SHORT" and evolution in {"PARTIAL_COVER", "FULL_COVER"}:
        if flow_imbalance < -0.25 or market_regime in {"bear", "risk_off", "crisis"}:
            confidence -= 0.04
            size_bias *= 0.80
            warnings.append("REDEYE_WRAPPER_EARLY_COVER_WARNING")

    # RedEye punishes long adds when flow is bearish.
    if action == "BUY" and transition in {"OPEN_LONG", "ADD_LONG"} and flow_imbalance < -0.20:
        confidence -= 0.08
        size_bias *= 0.65
        warnings.append("REDEYE_WRAPPER_BEARISH_FLOW_LONG_COMPRESSION")

    # RedEye likes downside when news shock is bearish, if evidence carries sentiment.
    sentiment_label = evidence.get("sentiment_label") or evidence.get("news_sentiment")
    if news_zscore >= 2.5 and sentiment_label == "bearish":
        if action == "SELL":
            confidence += 0.04
            reasons.append("REDEYE_WRAPPER_BEARISH_NEWS_SHOCK_SUPPORT")
        elif action == "BUY":
            confidence -= 0.06
            size_bias *= 0.70
            warnings.append("REDEYE_WRAPPER_BEARISH_NEWS_SHOCK_AGAINST_LONG")

    # Flips are allowed in spirit, but RedEye still compresses unless very strong.
    if transition in {"FLIP_LONG_TO_SHORT", "FLIP_SHORT_TO_LONG", "FLIP"}:
        if confidence >= 0.78:
            size_bias *= 0.75
            reasons.append("REDEYE_WRAPPER_HIGH_CONFIDENCE_FLIP_ALLOWED_COMPRESSED")
        else:
            confidence -= 0.10
            size_bias *= 0.45
            warnings.append("REDEYE_WRAPPER_LOW_CONFIDENCE_FLIP_COMPRESSED")

    if action == "HOLD":
        size_bias = 0.0

    # Squeeze Detector V2 (operator-shipped 2026-06-11). RedEye is the
    # adversary — when the squeeze is grade A (crowded long), RedEye
    # suspects the trade is consensus and compresses BUY confidence
    # while granting SELL confidence (waiting for the failed-breakout
    # short). The `already_fading_from_high` and `blowoff_velocity_risk`
    # flags directly support RedEye's contrarian-short thesis.
    snap = evidence.get("snapshot") or {}
    sq = snap.get("squeeze") or {}
    sq_grade = str(sq.get("grade") or "").upper()
    sq_risks = sq.get("risk_flags") or []
    if sq_grade == "A":
        if action == "BUY":
            confidence -= 0.05
            warnings.append("REDEYE_WRAPPER_SQUEEZE_A_CROWDED_LONG_SUSPECT")
        elif action == "SELL":
            confidence += 0.04
            reasons.append("REDEYE_WRAPPER_SQUEEZE_A_FAILED_BREAKOUT_OPPORTUNITY")
    if "already_fading_from_high" in sq_risks:
        if action == "SELL":
            confidence += 0.06
            reasons.append("REDEYE_WRAPPER_SQUEEZE_FADING_FROM_HIGH_SHORT_THESIS")
        elif action == "BUY":
            confidence -= 0.08
            size_bias *= 0.60
            warnings.append("REDEYE_WRAPPER_SQUEEZE_FADING_FROM_HIGH_NO_LONG")
    if "blowoff_velocity_risk" in sq_risks and action == "SELL":
        confidence += 0.05
        reasons.append("REDEYE_WRAPPER_SQUEEZE_BLOWOFF_REVERSAL_TARGET")
    if sq_grade == "F":
        # Stale or broken data: even RedEye won't act on it.
        confidence -= 0.08
        size_bias *= 0.55
        warnings.append("REDEYE_WRAPPER_SQUEEZE_DATA_ERROR_OR_STALE")

    evidence["legacy_wrapper"] = {
        "name": "redeye_legacy_adversary",
        "parent_brain": "redeye",
        "effect": "adversarial_short_pressure_and_consensus_challenge",
    }

    # ── Penalty-stacking dampener (2026-02-19 operator directive) ──
    size_bias, confidence, _damp = _finalise_size_and_confidence(
        final_size_bias=size_bias,
        final_confidence=confidence,
        base_size_bias=x["size_bias"],
        base_confidence=x["confidence"],
        action=action,
    )
    evidence["legacy_wrapper"]["dampener"] = _damp

    wrapped = WrappedIntent(
        brain_id=x["brain_id"],
        display_name=x["display_name"],
        wrapper="redeye_legacy_adversary",
        parent_brain="redeye",
        doctrine="opponent_adversary",
        action=action,
        confidence=round(confidence, 4),
        size_bias=round(size_bias, 4),
        current_side=current_side,
        transition_intent=transition,
        position_evolution=evolution,
        risk_transition=risk_transition,
        reasons=list(dict.fromkeys(reasons)),
        warnings=list(dict.fromkeys(warnings)),
        evidence=evidence,
    )

    return asdict(wrapped)



WRAPPER_REGISTRY = {
    "alpha_legacy_executor": apply_alpha_legacy_executor,
    "chevelle_legacy_governor": apply_chevelle_legacy_governor,
    "camaro_legacy_strategist": apply_camaro_legacy_strategist,
    "redeye_legacy_adversary": apply_redeye_legacy_adversary,
}


BRAIN_WRAPPER_ASSIGNMENTS = {
    "camino": "alpha_legacy_executor",
    "barracuda": "camaro_legacy_strategist",
    "hellcat": "chevelle_legacy_governor",
    "gto": "redeye_legacy_adversary",
}


# ── Operator-driven wrapper disable (2026-02-19) ────────────────────
#
# A/B diagnostic: when 403/502 rates spike on intent submit the
# operator wants to test whether the legacy wrappers are doing it
# (penalty-stacking dampener compresses size_bias toward zero;
# downstream cap_per_order then rejects). This toggle lets the
# operator switch off the wrapper for a SPECIFIC brain (one at a
# time, "disable AUDITOR for REDEYE only") and observe whether
# 403/502 frequency drops by ~25%.
#
# Sources of truth (highest precedence first):
#   1. In-process override set via `set_wrapper_disabled(brain_id, ...)`
#      from the admin endpoint — survives within the process; UI
#      driven, no restart needed.
#   2. `RISEDUAL_DISABLED_WRAPPERS` env var — comma-separated list
#      of brain_ids (e.g. "gto,hellcat"). Applies at process start.
#
# When a wrapper is disabled, `apply_legacy_wrapper` returns the
# intent unchanged but stamps `wrapper_disabled_by_operator: true`,
# `wrapper_disabled_reason`, and the original wrapper name on the
# intent so the audit feed shows WHICH intents skipped the wrapper.

import os as _os
import threading as _threading

_WRAPPER_DISABLE_LOCK = _threading.Lock()
_WRAPPER_DISABLED_OVERRIDES: dict[str, str] = {}


def _env_disabled_brains() -> set[str]:
    raw = _os.environ.get("RISEDUAL_DISABLED_WRAPPERS", "").strip()
    if not raw:
        return set()
    return {b.strip().lower() for b in raw.split(",") if b.strip()}


def is_wrapper_disabled(brain_id: str) -> tuple[bool, str]:
    """Return (disabled, reason). Reason is empty when enabled."""
    bid = (brain_id or "").lower().strip()
    if not bid:
        return False, ""
    with _WRAPPER_DISABLE_LOCK:
        if bid in _WRAPPER_DISABLED_OVERRIDES:
            return True, _WRAPPER_DISABLED_OVERRIDES[bid]
    if bid in _env_disabled_brains():
        return True, "env:RISEDUAL_DISABLED_WRAPPERS"
    return False, ""


def set_wrapper_disabled(brain_id: str, disabled: bool, reason: str = "") -> None:
    """Operator API — toggle a brain's legacy wrapper at runtime.

    `reason` is a short string the operator types when disabling
    (e.g. "A/B testing 403 cascade"). Stored alongside the disable
    flag so a future operator can answer "why is REDEYE's wrapper
    off?" from the audit panel.
    """
    bid = (brain_id or "").lower().strip()
    if bid not in BRAIN_WRAPPER_ASSIGNMENTS:
        raise ValueError(
            f"unknown brain_id {brain_id!r}; expected one of "
            f"{sorted(BRAIN_WRAPPER_ASSIGNMENTS)}"
        )
    with _WRAPPER_DISABLE_LOCK:
        if disabled:
            _WRAPPER_DISABLED_OVERRIDES[bid] = reason or "operator-toggled"
        else:
            _WRAPPER_DISABLED_OVERRIDES.pop(bid, None)


def wrapper_status() -> dict:
    """Snapshot for the admin UI / diagnostics. Returns each
    brain → wrapper assignment alongside its enabled/disabled state
    + reason."""
    env_disabled = _env_disabled_brains()
    with _WRAPPER_DISABLE_LOCK:
        overrides = dict(_WRAPPER_DISABLED_OVERRIDES)
    rows = []
    for brain, wrapper in BRAIN_WRAPPER_ASSIGNMENTS.items():
        if brain in overrides:
            rows.append({
                "brain_id": brain,
                "wrapper": wrapper,
                "disabled": True,
                "source": "operator_override",
                "reason": overrides[brain],
            })
        elif brain in env_disabled:
            rows.append({
                "brain_id": brain,
                "wrapper": wrapper,
                "disabled": True,
                "source": "env",
                "reason": "RISEDUAL_DISABLED_WRAPPERS",
            })
        else:
            rows.append({
                "brain_id": brain,
                "wrapper": wrapper,
                "disabled": False,
                "source": None,
                "reason": "",
            })
    return {
        "wrappers": rows,
        "env_disabled_brains": sorted(env_disabled),
        "override_count": len(overrides),
    }


def apply_legacy_wrapper(intent: dict[str, Any]) -> dict[str, Any]:
    """
    Generic wrapper entry point.

    If the brain has no wrapper assignment, returns the intent unchanged.

    If the brain's wrapper is operator-disabled (env var OR runtime
    override), returns the intent unchanged with audit fields stamped
    so the Intents feed shows which rows skipped the wrapper.

    ── Camino committee pre-pass (2026-02-21) ──
    Before any brain-specific wrapper runs, if the intent carries
    sub-agent votes at `evidence.committee_votes`, run the Alpha-
    weighted committee aggregator (see `shared/brains/camino_committee
    .py`). It REPLACES the intent's `confidence` with the calibrated
    weighted-mean of the votes (war_room 91.7% > signal_dispatcher
    66.4%, etc.) and stamps `evidence.committee_verdict` for audit.
    Downstream wrappers then do their normal discipline tweaks on
    top of the new confidence — net effect: the entire emit chain
    inherits Alpha's priors automatically.

    Kill switch: `RISEDUAL_CAMINO_COMMITTEE_DISABLED=1` env var.
    When no votes are attached, this pre-pass no-ops — existing
    Camino behaviour is bit-identical.
    """

    brain_id = str(intent.get("brain_id", "")).lower().strip()
    wrapper_name = BRAIN_WRAPPER_ASSIGNMENTS.get(brain_id)

    if not wrapper_name:
        return intent

    # Committee pre-pass — only when (a) this is Camino, (b) the kill
    # switch isn't tripped, and (c) the intent actually carries votes.
    if brain_id == "camino" and not _committee_kill_switch_tripped():
        try:
            from shared.brains.camino_committee import apply_committee_to_intent
            intent = apply_committee_to_intent(intent)
        except Exception as exc:  # noqa: BLE001
            # Committee MUST be fail-soft. If anything goes wrong,
            # stamp the audit field and let the legacy wrapper run
            # on the original confidence.
            ev = intent.setdefault("evidence", {})
            ev.setdefault("committee_error", repr(exc))

    # Camaro-weights pre-pass — Barracuda only. Mirrors the Camino
    # committee pre-pass: extracts council/regime/risk inputs from
    # the intent envelope, runs the upgraded `build_weighted_decision`
    # engine, writes back authoritative confidence + size_multiplier +
    # vetoes + conviction_score. Existing `apply_camaro_legacy_strategist`
    # then runs its position-aware refinements on top.
    if brain_id == "barracuda":
        try:
            from shared.brains.camaro_weights_adapter import (
                apply_camaro_weights_to_intent, kill_switch_tripped,
            )
            if not kill_switch_tripped():
                intent = apply_camaro_weights_to_intent(intent)
        except Exception as exc:  # noqa: BLE001
            ev = intent.setdefault("evidence", {})
            ev.setdefault("camaro_weights_import_error", repr(exc))

    disabled, reason = is_wrapper_disabled(brain_id)
    if disabled:
        # Don't mutate the caller's dict — return a copy with the
        # audit fields stamped.
        return {
            **intent,
            "wrapper_disabled_by_operator": True,
            "wrapper_disabled_source": (
                "operator_override" if reason and not reason.startswith("env:")
                else "env"
            ),
            "wrapper_disabled_reason": reason,
            "wrapper_skipped": wrapper_name,
        }

    wrapper = WRAPPER_REGISTRY[wrapper_name]
    return wrapper(intent)


def _committee_kill_switch_tripped() -> bool:
    """Sync env-var lookup for the Camino committee kill switch.
    Operator sets `RISEDUAL_CAMINO_COMMITTEE_DISABLED=1` to disable.
    """
    val = _os.environ.get("RISEDUAL_CAMINO_COMMITTEE_DISABLED", "").strip().lower()
    return val in {"1", "true", "yes", "on"}
