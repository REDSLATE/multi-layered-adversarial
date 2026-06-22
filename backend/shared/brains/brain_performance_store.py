"""Brain performance store — Mongo-backed (2026-02-21).

Replaces the operator's JSONL-on-disk design (`/var/lib/risedual/
brain_performance.jsonl`) with a query over the existing
`doctrine_sidecars` collection. Disk-on-pod is ephemeral on K8s —
we already have stack/lane/symbol/outcome_join.pnl_usd in Mongo,
no parallel store needed.

The function returns a `BrainPerformance` dataclass shaped
identically to the router's expectation, so `hot_brain_router.py`
sees no schema change.

Aggregation pipeline (lane + symbol filtered, last N trades):

  match: stack = brain AND lane = lane AND symbol = symbol
         AND outcome_join.pnl_usd exists
         AND outcome_join.joined_at >= cutoff
  sort: outcome_join.joined_at DESC
  limit: N
  → list of trade rows → run same metric math as operator's port

Returns sensible UNKNOWN defaults when no data — so the router
REDUCES new brains rather than BLOCKING them, per the operator's
"new brains must graduate" doctrine.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db import db
from shared.brains.hot_brain_router import BrainPerformance


DOCTRINE_SIDECARS = "doctrine_sidecars"
DEFAULT_LOOKBACK_TRADES = 20
DEFAULT_LOOKBACK_DAYS = 90


def _default_unknown(brain: str, lane: str, symbol: str) -> BrainPerformance:
    return BrainPerformance(
        brain=brain, lane=lane, symbol=symbol,
        trades=0, win_rate=0.0, avg_return_bps=0.0,
        profit_factor=1.0, max_drawdown_bps=0.0,
        streak_wins=0, streak_losses=0,
        last_trade_at=datetime.now(timezone.utc) - timedelta(days=365),
        lane_win_rate=0.0, symbol_win_rate=0.0,
    )


def _pnl_to_bps(pnl_usd: Any, notional_usd: Any) -> Optional[float]:
    """Approximate per-trade return in basis points.

    pnl_bps = (pnl_usd / notional_usd) * 10000.
    Returns None on missing/zero notional — caller skips the row.
    """
    try:
        pnl = float(pnl_usd)
    except (TypeError, ValueError):
        return None
    try:
        notional = float(notional_usd)
    except (TypeError, ValueError):
        notional = 0.0
    if notional <= 0:
        # No notional → can't normalize. Use raw pnl as a fallback unit
        # so the router can still see direction/magnitude crudely.
        return pnl * 100.0  # treat $1 P&L as 100 bps proxy
    return (pnl / notional) * 10000.0


async def _fetch_records(
    brain: str, lane: str, symbol: str, lookback: int,
) -> list[dict[str, Any]]:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    ).isoformat()
    cursor = db[DOCTRINE_SIDECARS].find(
        {
            "stack": brain,
            "lane": lane,
            "symbol": symbol,
            "outcome_join.pnl_usd": {"$exists": True},
            "outcome_join.joined_at": {"$gte": cutoff},
        },
        {
            "_id": 0,
            "outcome_join.pnl_usd": 1,
            "outcome_join.joined_at": 1,
            "outcome_join.notional_usd": 1,
            "stack": 1, "lane": 1, "symbol": 1,
        },
    ).sort("outcome_join.joined_at", -1).limit(max(1, lookback))
    return await cursor.to_list(length=lookback)


async def _fetch_lane_winrate(brain: str, lane: str) -> tuple[int, int]:
    """Lane-wide win/total over the 90d window for this brain."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    ).isoformat()
    pipeline = [
        {"$match": {
            "stack": brain, "lane": lane,
            "outcome_join.pnl_usd": {"$exists": True},
            "outcome_join.joined_at": {"$gte": cutoff},
        }},
        {"$group": {
            "_id": None,
            "trades": {"$sum": 1},
            "wins": {"$sum": {
                "$cond": [{"$gt": ["$outcome_join.pnl_usd", 0]}, 1, 0],
            }},
        }},
    ]
    async for row in db[DOCTRINE_SIDECARS].aggregate(pipeline):
        return int(row.get("wins", 0)), int(row.get("trades", 0))
    return 0, 0


def _aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the metrics the router expects from a list of trade rows."""
    if not records:
        return {}

    # Records came back sorted DESC by joined_at; re-sort ASC for
    # streak/drawdown calculations that read chronological order.
    rows = list(reversed(records))

    bps_list: list[float] = []
    last_trade_at = datetime.now(timezone.utc) - timedelta(days=365)
    for r in rows:
        oj = r.get("outcome_join") or {}
        bps = _pnl_to_bps(oj.get("pnl_usd"), oj.get("notional_usd"))
        if bps is None:
            continue
        bps_list.append(bps)
        ts_raw = oj.get("joined_at")
        if isinstance(ts_raw, str):
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                last_trade_at = max(last_trade_at, ts)
            except ValueError:
                pass

    if not bps_list:
        return {}

    trades = len(bps_list)
    wins = sum(1 for v in bps_list if v > 0)
    total_pnl = sum(bps_list)
    gross_profit = sum(v for v in bps_list if v > 0)
    gross_loss = sum(v for v in bps_list if v < 0)

    # Running drawdown (chronological).
    peak = 0.0
    running = 0.0
    max_dd = 0.0
    for v in bps_list:
        running += v
        peak = max(peak, running)
        dd = running - peak
        max_dd = min(max_dd, dd)

    # Streaks from end of sequence.
    streak_wins = 0
    streak_losses = 0
    for v in reversed(bps_list):
        if v > 0:
            if streak_losses > 0:
                break
            streak_wins += 1
        elif v < 0:
            if streak_wins > 0:
                break
            streak_losses += 1
        else:
            break

    win_rate = wins / trades
    avg_return_bps = total_pnl / trades
    if gross_loss != 0:
        profit_factor = abs(gross_profit / gross_loss)
    else:
        profit_factor = float("inf") if gross_profit > 0 else 0.0

    return {
        "trades": trades,
        "win_rate": win_rate,
        "avg_return_bps": avg_return_bps,
        "profit_factor": profit_factor,
        "max_drawdown_bps": max_dd,
        "streak_wins": streak_wins,
        "streak_losses": streak_losses,
        "last_trade_at": last_trade_at,
        "symbol_wins": wins,
        "symbol_trades": trades,
    }


async def get_recent_brain_performance(
    brain: str, lane: str, symbol: str,
    lookback: int = DEFAULT_LOOKBACK_TRADES,
) -> BrainPerformance:
    """Return BrainPerformance from Mongo. Defaults to UNKNOWN-grade
    when no trades exist so the router REDUCES (probes) rather than
    BLOCKS new brains."""
    if not (brain and lane and symbol):
        return _default_unknown(brain or "", lane or "", symbol or "")
    try:
        records = await _fetch_records(brain, lane, symbol, lookback)
        metrics = _aggregate(records)
        if not metrics:
            return _default_unknown(brain, lane, symbol)
        lane_wins, lane_trades = await _fetch_lane_winrate(brain, lane)
        lane_win_rate = (
            lane_wins / lane_trades if lane_trades > 0 else metrics["win_rate"]
        )
        symbol_win_rate = (
            metrics["symbol_wins"] / metrics["symbol_trades"]
            if metrics["symbol_trades"] > 0 else metrics["win_rate"]
        )
        return BrainPerformance(
            brain=brain, lane=lane, symbol=symbol,
            trades=metrics["trades"],
            win_rate=metrics["win_rate"],
            avg_return_bps=metrics["avg_return_bps"],
            profit_factor=metrics["profit_factor"],
            max_drawdown_bps=metrics["max_drawdown_bps"],
            streak_wins=metrics["streak_wins"],
            streak_losses=metrics["streak_losses"],
            last_trade_at=metrics["last_trade_at"],
            lane_win_rate=lane_win_rate,
            symbol_win_rate=symbol_win_rate,
        )
    except Exception:  # noqa: BLE001
        # Fail-soft: never let perf-store lookup break a caller.
        # Returning UNKNOWN means the router will REDUCE (probe).
        return _default_unknown(brain, lane, symbol)
