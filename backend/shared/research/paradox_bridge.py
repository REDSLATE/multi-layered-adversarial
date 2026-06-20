"""Paradox bridge — attach Research Layer output as *evidence* on an
existing brain intent.

This module is intentionally tiny and side-effect free. It does NOT:
  * submit anything to a broker
  * mutate gate / pipeline state
  * promote, demote, or otherwise modify the intent's action /
    confidence / direction

It only writes into `intent["evidence"]["research_signals"]` so the
post-mortem, the brain's prompt context, and the audit log can show
*what the analytical layer saw at emit time* — never *what it
decided*. Decision rights stay with the brain and the Seat / RoadGuard
chain.
"""
from __future__ import annotations

from typing import Iterable

from .schemas import StrategySignal


def attach_research_to_intent(
    intent: dict,
    signals: Iterable[StrategySignal],
) -> dict:
    """Stamp research signals into `intent["evidence"]["research_signals"]`.

    Idempotent — re-running with the same signals replaces the list
    rather than appending (so a brain that re-evaluates on the same
    bar doesn't bloat the evidence array).

    Returns the same `intent` dict the caller passed in, for chaining.
    """
    evidence = intent.setdefault("evidence", {})
    evidence["research_signals"] = [
        {
            "strategy_id": s.strategy_id,
            "direction": s.direction,
            "score": s.score,
            "confidence": s.confidence,
            "reasons": list(s.reasons),
        }
        for s in signals
    ]
    return intent
