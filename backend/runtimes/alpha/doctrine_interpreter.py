"""ALPHA · doctrine interpreter.

Alpha = strategist / opportunity seeker.
Doctrine may INCREASE or DECREASE conviction, but NEVER forces direction.
"""
from __future__ import annotations

from typing import Any, Dict

from shared.doctrine.base_labels import build_doctrine_labels


def interpret_for_alpha(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    doctrine = build_doctrine_labels(snapshot)
    labels = set(doctrine.labels)

    conviction_delta = 0.0

    if doctrine.quality == "A_QUALITY":
        conviction_delta += 0.12
    elif doctrine.quality == "B_QUALITY":
        conviction_delta += 0.05
    elif doctrine.quality == "REJECT":
        conviction_delta -= 0.20

    if "GAPPER" in labels and "HIGH_RELATIVE_VOLUME" in labels:
        conviction_delta += 0.06

    if "NEWS_CATALYST" in labels:
        conviction_delta += 0.04

    if "NO_NEWS_RISK" in labels:
        conviction_delta -= 0.06

    return {
        "brain": "camino",
        "role": "strategist",
        "doctrine": doctrine.to_dict(),
        "conviction_delta": round(conviction_delta, 4),
        "lesson": "Favor high-attention gappers with relative volume, catalyst, and clean pullback structure.",
        "may_execute": False,
        "may_override_direction": False,
    }
