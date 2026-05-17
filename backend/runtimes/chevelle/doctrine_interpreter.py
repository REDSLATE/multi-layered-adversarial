"""CHEVELLE · doctrine interpreter.

Chevelle = governor / risk.
Doctrine maps to RISK MODULATION or BLOCK recommendations.
"""
from __future__ import annotations

from typing import Any, Dict

from shared.doctrine.base_labels import build_doctrine_labels


def interpret_for_chevelle(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    doctrine = build_doctrine_labels(snapshot)
    labels = set(doctrine.labels)

    risk_multiplier = 1.0
    block_reasons = []

    consecutive_losses = int(snapshot.get("consecutive_losses", 0))
    daily_pnl = float(snapshot.get("daily_pnl", 0.0))

    if doctrine.quality == "A_QUALITY":
        risk_multiplier *= 1.00
    elif doctrine.quality == "B_QUALITY":
        risk_multiplier *= 0.75
    elif doctrine.quality == "C_QUALITY":
        risk_multiplier *= 0.50
    else:
        risk_multiplier *= 0.00
        block_reasons.append("doctrine_reject")

    if "MARKET_WEAK_REDUCE_RISK" in labels:
        risk_multiplier *= 0.50
    if "SPREAD_TOO_WIDE" in labels:
        risk_multiplier *= 0.50

    if consecutive_losses >= 3:
        risk_multiplier = 0.0
        block_reasons.append("three_consecutive_losses")
    if daily_pnl <= -100:
        risk_multiplier = 0.0
        block_reasons.append("daily_max_loss_reached")

    risk_multiplier = max(0.0, min(1.0, risk_multiplier))

    return {
        "brain": "chevelle",
        "role": "governor",
        "doctrine": doctrine.to_dict(),
        "risk_multiplier": round(risk_multiplier, 4),
        "governor_action": "block" if risk_multiplier == 0.0 else "modulate",
        "block_reasons": block_reasons,
        "lesson": "Reduce or block risk when setup quality, market regime, spread, or loss limits are unfavorable.",
        "may_execute": False,
        "may_override_direction": False,
    }
