"""Deterministic Stop-Loss Guard.

Doctrine (2026-02-16):
    Capital protection. Higher priority than every other guard — runs
    FIRST in the Position Monitor loop. Brain advisory cannot override.

    Pure math. Lane-neutral. No DB, no async, no LLM.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Side = Literal["LONG", "SHORT"]
Action = Literal["HOLD", "CLOSE"]


@dataclass(frozen=True)
class StopLossVerdict:
    action: Action
    reason: str
    pnl_pct: float
    target_pct: float
    close_fraction: float


def calc_unrealized_pct(*, side: Side, entry_price: float, current_price: float) -> float:
    if entry_price <= 0:
        return 0.0
    if side == "LONG":
        return ((current_price - entry_price) / entry_price) * 100.0
    if side == "SHORT":
        return ((entry_price - current_price) / entry_price) * 100.0
    return 0.0


def stop_loss_guard(
    *,
    side: Side,
    entry_price: float,
    current_price: float,
    stop_loss_pct: float = 2.0,
) -> StopLossVerdict:
    """Returns CLOSE when pnl_pct <= -stop_loss_pct (loss exceeds cap).
    Threshold is the *magnitude* of the loss — pass `stop_loss_pct=2.0`
    to mean "close when down 2% or more".
    """
    pnl_pct = calc_unrealized_pct(
        side=side, entry_price=entry_price, current_price=current_price,
    )
    if pnl_pct <= -abs(stop_loss_pct):
        return StopLossVerdict(
            action="CLOSE",
            reason=f"Stop-loss hit at -{abs(stop_loss_pct):.2f}% (pnl={pnl_pct:.2f}%)",
            pnl_pct=round(pnl_pct, 4),
            target_pct=-abs(stop_loss_pct),
            close_fraction=1.0,
        )
    return StopLossVerdict(
        action="HOLD",
        reason="Stop-loss target not hit.",
        pnl_pct=round(pnl_pct, 4),
        target_pct=-abs(stop_loss_pct),
        close_fraction=0.0,
    )
