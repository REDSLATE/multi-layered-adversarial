"""Deterministic Max-Hold-Time Guard.

Doctrine (2026-02-16):
    Stale-thesis hygiene. If a position has been open longer than the
    configured maximum hold time and the thesis hasn't matured into
    a take-profit or stop-loss event, close it — runaway exposure is
    worse than a small scratch outcome.

    Pure math. Lane-neutral. No DB, no async, no LLM.

    Lowest priority in the Position Monitor loop — runs AFTER stop-loss,
    take-profit, and trailing-stop. The other three are about market
    structure; this one is about discipline.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional


Action = Literal["HOLD", "CLOSE"]


@dataclass(frozen=True)
class MaxHoldVerdict:
    action: Action
    reason: str
    held_for_minutes: float
    target_minutes: float
    close_fraction: float


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        # Mongo stores ISO strings with trailing 'Z' or '+00:00'.
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def max_hold_time_guard(
    *,
    opened_at: str,
    max_hold_minutes: float = 60.0 * 24.0,   # default: 24h
    now: Optional[datetime] = None,
) -> MaxHoldVerdict:
    """Returns CLOSE when (now - opened_at) >= max_hold_minutes.

    `opened_at` is an ISO string (UTC). `now` is exposed so tests can
    pin time. Production callers leave `now=None` to use the system
    clock.
    """
    opened = _parse_iso(opened_at)
    if opened is None:
        return MaxHoldVerdict(
            action="HOLD",
            reason="Invalid opened_at — cannot evaluate.",
            held_for_minutes=0.0,
            target_minutes=max_hold_minutes,
            close_fraction=0.0,
        )

    current = now or datetime.now(timezone.utc)
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)

    held_minutes = (current - opened).total_seconds() / 60.0

    if held_minutes >= max_hold_minutes:
        return MaxHoldVerdict(
            action="CLOSE",
            reason=(
                f"Max hold time exceeded: held {held_minutes:.1f}m "
                f"≥ cap {max_hold_minutes:.1f}m"
            ),
            held_for_minutes=round(held_minutes, 2),
            target_minutes=max_hold_minutes,
            close_fraction=1.0,
        )

    return MaxHoldVerdict(
        action="HOLD",
        reason=(
            f"Within hold window ({held_minutes:.1f}m of {max_hold_minutes:.1f}m)."
        ),
        held_for_minutes=round(held_minutes, 2),
        target_minutes=max_hold_minutes,
        close_fraction=0.0,
    )
