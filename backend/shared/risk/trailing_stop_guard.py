"""Deterministic Trailing-Stop Guard.

Doctrine (2026-02-16):
    Let winners run, but cut them when they give back too much from
    the peak. Stateful — tracks the high-water mark per position
    across ticks. Pure math here; caller persists `peak_price` on the
    position doc and feeds it back in on the next tick.

    Lane-neutral. No DB, no async, no LLM.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


Side = Literal["LONG", "SHORT"]
Action = Literal["HOLD", "CLOSE"]


@dataclass(frozen=True)
class TrailingStopVerdict:
    action: Action
    reason: str
    new_peak: float           # high-water for LONG, low-water for SHORT
    pnl_from_peak_pct: float  # always negative or zero
    target_pct: float
    close_fraction: float


def trailing_stop_guard(
    *,
    side: Side,
    entry_price: float,
    current_price: float,
    previous_peak: Optional[float] = None,
    trail_pct: float = 1.5,
    activate_after_pct: float = 1.0,
) -> TrailingStopVerdict:
    """Returns CLOSE when current_price has dropped (LONG) or risen
    (SHORT) by `trail_pct%` from the running peak. The guard does NOT
    activate until the position is up by at least `activate_after_pct`
    — otherwise it would trigger on normal entry noise.

    For LONG positions, `previous_peak` is the highest price seen since
    open (default = entry_price). For SHORT positions, it's the lowest.
    Caller persists the returned `new_peak` for the next tick.
    """
    if entry_price <= 0 or current_price <= 0:
        return TrailingStopVerdict(
            action="HOLD",
            reason="Invalid price.",
            new_peak=current_price,
            pnl_from_peak_pct=0.0,
            target_pct=trail_pct,
            close_fraction=0.0,
        )

    if side == "LONG":
        peak = previous_peak if (previous_peak is not None and previous_peak > 0) else entry_price
        new_peak = max(peak, current_price)
        # Has the position even reached the activation threshold?
        peak_gain_pct = ((new_peak - entry_price) / entry_price) * 100.0
        if peak_gain_pct < activate_after_pct:
            return TrailingStopVerdict(
                action="HOLD",
                reason=(
                    f"Trailing stop inactive — peak gain {peak_gain_pct:.2f}% "
                    f"below activation {activate_after_pct:.2f}%"
                ),
                new_peak=new_peak,
                pnl_from_peak_pct=0.0,
                target_pct=trail_pct,
                close_fraction=0.0,
            )
        # Drawdown from peak
        from_peak = ((current_price - new_peak) / new_peak) * 100.0
        if from_peak <= -abs(trail_pct):
            return TrailingStopVerdict(
                action="CLOSE",
                reason=(
                    f"Trailing-stop hit: -{abs(trail_pct):.2f}% from peak "
                    f"(peak={new_peak:.4f}, current={current_price:.4f})"
                ),
                new_peak=new_peak,
                pnl_from_peak_pct=round(from_peak, 4),
                target_pct=-abs(trail_pct),
                close_fraction=1.0,
            )
        return TrailingStopVerdict(
            action="HOLD",
            reason="Within trailing band.",
            new_peak=new_peak,
            pnl_from_peak_pct=round(from_peak, 4),
            target_pct=-abs(trail_pct),
            close_fraction=0.0,
        )

    if side == "SHORT":
        trough = previous_peak if (previous_peak is not None and previous_peak > 0) else entry_price
        new_trough = min(trough, current_price)
        trough_gain_pct = ((entry_price - new_trough) / entry_price) * 100.0
        if trough_gain_pct < activate_after_pct:
            return TrailingStopVerdict(
                action="HOLD",
                reason=(
                    f"Trailing stop inactive — peak gain {trough_gain_pct:.2f}% "
                    f"below activation {activate_after_pct:.2f}%"
                ),
                new_peak=new_trough,
                pnl_from_peak_pct=0.0,
                target_pct=trail_pct,
                close_fraction=0.0,
            )
        from_peak = ((new_trough - current_price) / new_trough) * 100.0  # negative when price rises against short
        if from_peak <= -abs(trail_pct):
            return TrailingStopVerdict(
                action="CLOSE",
                reason=(
                    f"Trailing-stop hit: -{abs(trail_pct):.2f}% from trough "
                    f"(trough={new_trough:.4f}, current={current_price:.4f})"
                ),
                new_peak=new_trough,
                pnl_from_peak_pct=round(from_peak, 4),
                target_pct=-abs(trail_pct),
                close_fraction=1.0,
            )
        return TrailingStopVerdict(
            action="HOLD",
            reason="Within trailing band.",
            new_peak=new_trough,
            pnl_from_peak_pct=round(from_peak, 4),
            target_pct=-abs(trail_pct),
            close_fraction=0.0,
        )

    return TrailingStopVerdict(
        action="HOLD", reason="Unknown side.", new_peak=current_price,
        pnl_from_peak_pct=0.0, target_pct=trail_pct, close_fraction=0.0,
    )
