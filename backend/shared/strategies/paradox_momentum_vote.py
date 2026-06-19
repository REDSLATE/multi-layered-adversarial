"""Paradox MA-momentum canary signal.

This module produces a deterministic moving-average crossover signal
solely to PROVE the Paradox plumbing end-to-end:

    external deterministic signal
        → BrainOpinion adapter
        → /api/ingest
        → shared_intents
        → SeatPolicy → Governor → RoadGuard → Broker
        → traceable receipt in pipeline_receipts

The win condition is NOT "MA strategy makes money" — it's that an
external strategy can enter the system and produce a named receipt.

Doctrine pin: strategy testifies, never executes. This module does
NOT call the broker directly. It hands a vote to Paradox and lets
Paradox decide. The 2026-06-19 ship deliberately defaults the kill
switch OFF (`PARADOX_MA_CANARY_ENABLED=false`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal


Action = Literal["BUY", "SELL", "HOLD"]
Lane = Literal["equity", "crypto"]


@dataclass(frozen=True)
class MomentumSignal:
    symbol: str
    lane: Lane
    action: Action
    confidence: float
    fast_ma: float
    slow_ma: float
    ma_gap_pct: float
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


def _clamp_01(value: float) -> float:
    if math.isnan(value) or math.isinf(value):
        return 0.0
    return max(0.0, min(1.0, value))


def moving_average_momentum(
    symbol: str,
    closes: list[float],
    lane: Lane = "equity",
    fast_window: int = 10,
    slow_window: int = 30,
    min_confidence: float = 0.05,
) -> MomentumSignal:
    """Fast/slow SMA crossover. BUY when fast > slow, SELL when fast < slow.

    Confidence scales linearly with the absolute MA gap:
        1% gap ≈ 0.25 confidence
        4%+ gap ≈ 1.00 confidence (clamped)

    Returns HOLD if there aren't enough bars, the slow MA degenerates,
    or the gap is below `min_confidence`. HOLD is the only safe state
    when the signal can't be honestly computed.
    """
    clean_closes = [float(x) for x in closes if x is not None and float(x) > 0]

    if len(clean_closes) < slow_window:
        return MomentumSignal(
            symbol=symbol, lane=lane, action="HOLD", confidence=0.0,
            fast_ma=0.0, slow_ma=0.0, ma_gap_pct=0.0,
            reason="not_enough_history",
            evidence={
                "strategy": "moving_average_momentum",
                "required_bars": slow_window,
                "received_bars": len(clean_closes),
            },
        )

    fast_ma = sum(clean_closes[-fast_window:]) / fast_window
    slow_ma = sum(clean_closes[-slow_window:]) / slow_window

    if slow_ma <= 0:
        return MomentumSignal(
            symbol=symbol, lane=lane, action="HOLD", confidence=0.0,
            fast_ma=fast_ma, slow_ma=slow_ma, ma_gap_pct=0.0,
            reason="invalid_slow_ma",
            evidence={"strategy": "moving_average_momentum"},
        )

    ma_gap_pct = ((fast_ma - slow_ma) / slow_ma) * 100.0
    confidence = _clamp_01(abs(ma_gap_pct) / 4.0)

    if confidence < min_confidence:
        action: Action = "HOLD"
        reason = "ma_gap_too_small"
    elif fast_ma > slow_ma:
        action = "BUY"
        reason = "fast_ma_above_slow_ma"
    elif fast_ma < slow_ma:
        action = "SELL"
        reason = "fast_ma_below_slow_ma"
    else:
        action = "HOLD"
        reason = "ma_flat"

    return MomentumSignal(
        symbol=symbol, lane=lane, action=action, confidence=confidence,
        fast_ma=fast_ma, slow_ma=slow_ma, ma_gap_pct=ma_gap_pct,
        reason=reason,
        evidence={
            "strategy": "moving_average_momentum",
            "fast_window": fast_window,
            "slow_window": slow_window,
            "bars_used": len(clean_closes),
            "latest_close": clean_closes[-1],
        },
    )
