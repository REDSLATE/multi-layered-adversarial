"""
REDEYE → Camaro short-side bridge.

Purpose:
- REDEYE specializes in bearish/short-side detection.
- REDEYE reports to Camaro, not Alpha.
- Camaro remains the final execution authority.
- REDEYE cannot place orders directly.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional


MIN_SHORT_SCORE = 0.70
MIN_BEAR_CONFIDENCE = 0.62

MAX_REDEYE_RISK_MULTIPLIER = 0.75
MIN_REDEYE_RISK_MULTIPLIER = 0.25


@dataclass
class RedeyeShortSignal:
    engine: str
    symbol: str
    side: str
    action: str
    bear_score: float
    confidence: float
    risk_multiplier: float
    allowed: bool
    reason: str
    reports_to: str
    created_at: str
    raw: Dict[str, Any]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def build_redeye_short_signal(
    symbol: str,
    features: Dict[str, Any],
    *,
    model_score: Optional[float] = None,
) -> RedeyeShortSignal:
    """
    Converts market features into a REDEYE short-side advisory signal.

    This does NOT execute trades.
    This does NOT report to Alpha.
    This only reports to Camaro.
    """

    price_change_pct = float(features.get("price_change_pct", 0.0) or 0.0)
    rsi = float(features.get("rsi_14", 50.0) or 50.0)
    macd_hist = float(features.get("macd_hist", 0.0) or 0.0)
    volume_ratio = float(features.get("volume_ratio", 1.0) or 1.0)
    below_sma_20 = bool(features.get("below_sma_20", False))
    below_sma_50 = bool(features.get("below_sma_50", False))
    failed_bounce = bool(features.get("failed_bounce", False))
    liquidity_ok = bool(features.get("liquidity_ok", True))
    borrow_ok = bool(features.get("borrow_ok", True))

    score = 0.0
    reasons = []

    if price_change_pct < -1.0:
        score += 0.15
        reasons.append("negative_price_momentum")

    if rsi < 45:
        score += 0.12
        reasons.append("weak_rsi")

    if macd_hist < 0:
        score += 0.15
        reasons.append("bearish_macd")

    if volume_ratio > 1.25:
        score += 0.12
        reasons.append("selling_volume_expansion")

    if below_sma_20:
        score += 0.12
        reasons.append("below_sma_20")

    if below_sma_50:
        score += 0.14
        reasons.append("below_sma_50")

    if failed_bounce:
        score += 0.15
        reasons.append("failed_bounce")

    if model_score is not None:
        score = (score * 0.55) + (float(model_score) * 0.45)
        reasons.append("redeye_model_score_blended")

    bear_score = clamp(score, 0.0, 1.0)

    confidence = clamp(
        0.35 + bear_score * 0.65,
        0.0,
        1.0,
    )

    blocked_reasons = []

    if not liquidity_ok:
        blocked_reasons.append("liquidity_block")

    if not borrow_ok:
        blocked_reasons.append("borrow_block")

    allowed = (
        bear_score >= MIN_SHORT_SCORE
        and confidence >= MIN_BEAR_CONFIDENCE
        and not blocked_reasons
    )

    if allowed:
        action = "SHORT"
        risk_multiplier = clamp(
            bear_score,
            MIN_REDEYE_RISK_MULTIPLIER,
            MAX_REDEYE_RISK_MULTIPLIER,
        )
    else:
        action = "HOLD"
        risk_multiplier = 0.0

    reason = "+".join(reasons or ["no_short_edge"])

    if blocked_reasons:
        reason = reason + "|" + "+".join(blocked_reasons)

    return RedeyeShortSignal(
        engine="REDEYE",
        symbol=symbol.upper(),
        side="SHORT",
        action=action,
        bear_score=round(bear_score, 4),
        confidence=round(confidence, 4),
        risk_multiplier=round(risk_multiplier, 4),
        allowed=allowed,
        reason=reason,
        reports_to="CAMARO",
        created_at=datetime.now(timezone.utc).isoformat(),
        raw={
            "features": features,
            "model_score": model_score,
        },
    )


def export_for_camaro(signal: RedeyeShortSignal) -> Dict[str, Any]:
    """
    Payload Camaro can consume.

    Camaro must still make the final decision.
    """
    payload = asdict(signal)

    payload["camaro_contract"] = {
        "source": "REDEYE",
        "role": "short_side_advisor",
        "may_execute": False,
        "may_override_alpha": False,
        "final_authority": "CAMARO",
    }

    return payload
