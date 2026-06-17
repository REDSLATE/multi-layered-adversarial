"""CAMARO · doctrine interpreter.

Camaro = execution judge.
Doctrine affects EXECUTION READINESS only.
It does NOT create direction.
"""
from __future__ import annotations

from typing import Any, Dict

from shared.doctrine.base_labels import build_doctrine_labels


def interpret_for_camaro(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    doctrine = build_doctrine_labels(snapshot)
    labels = set(doctrine.labels)

    execution_checks = {
        "quality_ok": doctrine.quality in {"A_QUALITY", "B_QUALITY"},
        "spread_ok": "SPREAD_ACCEPTABLE" in labels,
        "market_not_weak": "MARKET_WEAK_REDUCE_RISK" not in labels,
        "has_attention": "GAPPER" in labels or "HIGH_RELATIVE_VOLUME" in labels,
    }

    execution_ready = all(execution_checks.values())

    return {
        "brain": "barracuda",
        "role": "execution_judge",
        "doctrine": doctrine.to_dict(),
        "execution_ready": execution_ready,
        "execution_checks": execution_checks,
        "lesson": "Only execute after independent direction exists and setup quality, spread, attention, and regime are acceptable.",
        "may_execute": False,
        "may_create_direction": False,
        "requires_existing_trade_intent": True,
    }
