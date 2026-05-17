"""Deterministic Take-Profit Guard.

Doctrine (2026-02-16):
    Executors may enter. Lifecycle guards may exit. Brains may advise.
    RoadGuard enforces.

    TakeProfitGuard is a deterministic post-entry lifecycle guard. It
    is higher-priority than executor hesitation — brains cannot override
    a take-profit exit. Given an open position's side / entry / current
    price, the guard returns a verdict (HOLD / REDUCE / CLOSE). The
    caller is responsible for actually executing the close through the
    lane-appropriate broker adapter.

    Pure math. No LLM. No DB. No async. Lane-agnostic. The same module
    is consumed by `shared/equity/take_profit.py` and
    `shared/crypto/take_profit.py`, which add the lane-specific wiring
    on top.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Side = Literal["LONG", "SHORT"]
Action = Literal["HOLD", "CLOSE", "REDUCE"]


@dataclass(frozen=True)
class TakeProfitVerdict:
    action: Action
    reason: str
    pnl_pct: float
    target_pct: float
    close_fraction: float


def calc_unrealized_pct(
    *,
    side: Side,
    entry_price: float,
    current_price: float,
) -> float:
    if entry_price <= 0:
        return 0.0

    if side == "LONG":
        return ((current_price - entry_price) / entry_price) * 100.0

    if side == "SHORT":
        return ((entry_price - current_price) / entry_price) * 100.0

    return 0.0


def take_profit_guard(
    *,
    side: Side,
    entry_price: float,
    current_price: float,
    take_profit_pct: float = 3.0,
    partial_take_pct: float | None = None,
    partial_close_fraction: float = 0.50,
) -> TakeProfitVerdict:
    pnl_pct = calc_unrealized_pct(
        side=side,
        entry_price=entry_price,
        current_price=current_price,
    )

    # Optional first partial take-profit
    if partial_take_pct is not None and pnl_pct >= partial_take_pct and pnl_pct < take_profit_pct:
        return TakeProfitVerdict(
            action="REDUCE",
            reason=f"Partial take-profit hit at {partial_take_pct:.2f}%",
            pnl_pct=round(pnl_pct, 4),
            target_pct=partial_take_pct,
            close_fraction=partial_close_fraction,
        )

    # Full take-profit
    if pnl_pct >= take_profit_pct:
        return TakeProfitVerdict(
            action="CLOSE",
            reason=f"Take-profit target hit at {take_profit_pct:.2f}%",
            pnl_pct=round(pnl_pct, 4),
            target_pct=take_profit_pct,
            close_fraction=1.0,
        )

    return TakeProfitVerdict(
        action="HOLD",
        reason="Take-profit target not hit.",
        pnl_pct=round(pnl_pct, 4),
        target_pct=take_profit_pct,
        close_fraction=0.0,
    )
