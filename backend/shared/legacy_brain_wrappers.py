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
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Literal


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

    wrapped = WrappedIntent(
        brain_id=x["brain_id"],
        display_name=x["display_name"],
        wrapper="alpha_legacy_executor",
        parent_brain="alpha",
        doctrine="executor_confirming",
        action=action,
        confidence=round(clamp(confidence), 4),
        size_bias=round(clamp(size_bias, 0.0, 2.0), 4),
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

    wrapped = WrappedIntent(
        brain_id=x["brain_id"],
        display_name=x["display_name"],
        wrapper="chevelle_legacy_governor",
        parent_brain="chevelle",
        doctrine="adaptive_governor",
        action=action,
        confidence=round(clamp(confidence), 4),
        size_bias=round(clamp(size_bias, 0.0, 2.0), 4),
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

    evidence["legacy_wrapper"] = {
        "name": "camaro_legacy_strategist",
        "parent_brain": "camaro",
        "effect": "live_market_tape_reading_and_continuation_bias",
    }

    wrapped = WrappedIntent(
        brain_id=x["brain_id"],
        display_name=x["display_name"],
        wrapper="camaro_legacy_strategist",
        parent_brain="camaro",
        doctrine="live_market_strategist",
        action=action,
        confidence=round(clamp(confidence), 4),
        size_bias=round(clamp(size_bias, 0.0, 2.0), 4),
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

    evidence["legacy_wrapper"] = {
        "name": "redeye_legacy_adversary",
        "parent_brain": "redeye",
        "effect": "adversarial_short_pressure_and_consensus_challenge",
    }

    wrapped = WrappedIntent(
        brain_id=x["brain_id"],
        display_name=x["display_name"],
        wrapper="redeye_legacy_adversary",
        parent_brain="redeye",
        doctrine="opponent_adversary",
        action=action,
        confidence=round(clamp(confidence), 4),
        size_bias=round(clamp(size_bias, 0.0, 2.0), 4),
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


def apply_legacy_wrapper(intent: dict[str, Any]) -> dict[str, Any]:
    """
    Generic wrapper entry point.

    If the brain has no wrapper assignment, returns the intent unchanged.
    """

    brain_id = str(intent.get("brain_id", "")).lower().strip()
    wrapper_name = BRAIN_WRAPPER_ASSIGNMENTS.get(brain_id)

    if not wrapper_name:
        return intent

    wrapper = WRAPPER_REGISTRY[wrapper_name]
    return wrapper(intent)
