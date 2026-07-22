"""Rise Kernel throttle — live wiring (2026-07-22).

The Kernel (`shared/brains/hot_brain_router.py`) was DORMANT: the
arbiter multiplied by a hardcoded `KERNEL_MULTIPLIER_DEFAULT = 1.0`
and the performance store read `doctrine_sidecars` — a collection
dead since the sidecars were decommissioned. This module feeds the
Kernel's UNCHANGED hot-score math from `shared_exit_outcomes`
(realized round-trips, brain-attributed) and maps the score to a
size multiplier:

    multiplier = min_mult + hot_score * (max_mult - min_mult)
    (defaults [0.50, 1.35] — operator spec)

Doctrine: THROTTLE, NEVER VETO. Route actions (BLOCK etc.) are not
consulted — only the score. Cold start (no realized outcomes yet)
→ neutral 1.0, because modifying size on zero data is noise.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import db
from shared.brains.hot_brain_router import BrainPerformance, compute_hot_score

logger = logging.getLogger("risedual.kernel_throttle")

EXIT_OUTCOMES = "shared_exit_outcomes"
LOOKBACK_TRADES = 20
LOOKBACK_DAYS = 90
_CACHE_TTL_S = 60.0

_cache: dict[str, tuple[float, dict]] = {}


async def performance_from_outcomes(
    brain: str, lane: str,
) -> Optional[BrainPerformance]:
    """BrainPerformance from realized exits. None = no data (cold)."""
    since = (
        datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    ).isoformat()
    rows = await (
        db[EXIT_OUTCOMES]
        .find(
            {"brain": brain, "lane": lane, "closed_at": {"$gte": since},
             "realized_pnl_pct": {"$ne": None}},
            {"_id": 0, "realized_pnl_pct": 1, "realized_pnl_usd": 1,
             "closed_at": 1},
        )
        .sort("closed_at", -1)
        .limit(LOOKBACK_TRADES)
        .to_list(LOOKBACK_TRADES)
    )
    if not rows:
        return None

    pnls = [float(r["realized_pnl_pct"]) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / len(pnls)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (
        3.0 if gross_win > 0 else 1.0
    )
    # Streaks — rows are newest-first.
    streak_wins = streak_losses = 0
    for p in pnls:
        if p > 0 and streak_losses == 0:
            streak_wins += 1
        elif p <= 0 and streak_wins == 0:
            streak_losses += 1
        else:
            break
    last_at = datetime.fromisoformat(str(rows[0]["closed_at"]))
    return BrainPerformance(
        brain=brain, lane=lane, symbol="*",
        trades=len(pnls),
        win_rate=win_rate,
        avg_return_bps=(sum(pnls) / len(pnls)) * 100.0,
        profit_factor=profit_factor,
        max_drawdown_bps=abs(min(pnls, default=0.0)) * 100.0,
        streak_wins=streak_wins,
        streak_losses=streak_losses,
        last_trade_at=last_at,
        lane_win_rate=win_rate,
        symbol_win_rate=win_rate,
    )


async def get_kernel_throttle(brain: str, lane: str) -> dict:
    """{multiplier, score, state, trades}. Fail-soft neutral."""
    key = f"{brain}:{lane}"
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and (now - hit[0]) < _CACHE_TTL_S:
        return hit[1]

    from shared.opportunity.policy import get_opportunity_policy  # noqa: WPS433
    try:
        policy = (await get_opportunity_policy())["kernel"]
        if not policy["enabled"]:
            out = {"multiplier": 1.0, "score": None,
                   "state": "disabled", "trades": 0}
            _cache[key] = (now, out)
            return out
        perf = await performance_from_outcomes(brain, lane)
        if perf is None:
            out = {"multiplier": 1.0, "score": None,
                   "state": "cold_start_neutral", "trades": 0}
        else:
            score = compute_hot_score(perf)
            lo, hi = policy["min_mult"], policy["max_mult"]
            out = {
                "multiplier": round(lo + score * (hi - lo), 4),
                "score": round(score, 4),
                "state": "live",
                "trades": perf.trades,
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("kernel throttle failed %s/%s: %s", brain, lane, exc)
        out = {"multiplier": 1.0, "score": None, "state": "error", "trades": 0}
    _cache[key] = (now, out)
    return out


def invalidate_kernel_cache() -> None:
    _cache.clear()
