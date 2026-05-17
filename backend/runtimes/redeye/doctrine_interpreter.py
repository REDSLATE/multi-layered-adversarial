"""REDEYE · doctrine interpreter.

REDEYE = adversarial challenger.
Doctrine produces OBJECTIONS / CHALLENGES, not execution.
"""
from __future__ import annotations

from typing import Any, Dict

from shared.doctrine.base_labels import build_doctrine_labels


def interpret_for_redeye(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    doctrine = build_doctrine_labels(snapshot)
    labels = set(doctrine.labels)

    objections = []

    if "NO_NEWS_RISK" in labels:
        objections.append("move_not_news_backed")
    if "SPREAD_TOO_WIDE" in labels:
        objections.append("spread_risk")
    if "MARKET_WEAK_REDUCE_RISK" in labels:
        objections.append("weak_market_regime")
    if doctrine.quality in {"C_QUALITY", "REJECT"}:
        objections.append("setup_quality_insufficient")
    if "LOW_FLOAT_SUPPLY_IMBALANCE" not in labels:
        objections.append("supply_imbalance_not_confirmed")

    challenge_strength = min(1.0, 0.20 + 0.18 * len(objections))

    return {
        "brain": "redeye",
        "role": "adversary",
        "doctrine": doctrine.to_dict(),
        "challenge_required": bool(objections),
        "challenge_strength": round(challenge_strength, 4),
        "objections": objections,
        "lesson": "Attack weak setups, fake momentum, no-news moves, poor spreads, and weak regimes.",
        "may_execute": False,
        "may_override_direction": False,
    }
