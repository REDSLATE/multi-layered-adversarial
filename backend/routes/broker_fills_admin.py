"""Admin routes for `shared_broker_fills` — the broker-truth feed.

Doctrine pin (operator directive, 2026-06-10): this is the first
place an operator can see "what Public.com actually did" vs
"what MC's intent log claims it did." The collection is populated
by `shared.broker_fills.start_broker_fills_poller`.

Endpoints (all JWT-admin-gated):
    GET /api/admin/broker-fills/recent
        ?symbol=AAPL&minutes=60&limit=200

    GET /api/admin/broker-fills/pending/{symbol}
        Returns fills inside the auto-router's "in flight" window —
        the source of truth for the dedupe check next pass.

    GET /api/admin/broker-fills/summary
        ?minutes=60
        Per-symbol aggregate: count, side breakdown, total qty,
        total notional, last fill timestamp. The dashboard's
        broker-truth gauge reads from this.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from shared.broker_fills import (
    get_pending_orders_for_symbol,
    get_recent_fills,
)
from shared.in_flight_orders import snapshot as in_flight_snapshot


router = APIRouter(prefix="/admin/broker-fills", tags=["admin-broker-fills"])


@router.get("/recent")
async def recent_fills(
    symbol: Optional[str] = Query(None, description="Filter by symbol, e.g. AAPL"),
    minutes: int = Query(60, ge=1, le=1440),
    limit: int = Query(200, ge=1, le=1000),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Most recent broker fills, newest first.

    Without `symbol`, returns the cross-symbol feed. With `symbol`,
    returns only fills for that ticker. The `minutes` parameter is
    a trailing window over the fill's broker timestamp (NOT MC's
    ingest time).
    """
    rows = await get_recent_fills(
        symbol=symbol, minutes=minutes, limit=limit,
    )
    return {
        "symbol": (symbol.upper() if symbol else None),
        "window_minutes": minutes,
        "count": len(rows),
        "fills": rows,
    }


@router.get("/pending/{symbol}")
async def pending_for_symbol(
    symbol: str,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Return fills inside the auto-router's pending TTL window.

    Doctrine: this is the dedupe oracle. If this returns a non-empty
    list, the auto-router MUST NOT submit another order on this
    symbol — there's already one in flight or just acknowledged.
    """
    rows = await get_pending_orders_for_symbol(symbol)
    return {
        "symbol": symbol.upper(),
        "pending_count": len(rows),
        "pending": rows,
    }


@router.get("/summary")
async def summary(
    minutes: int = Query(60, ge=1, le=1440),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Per-symbol aggregate over the trailing window.

    Returns one row per symbol with:
        count          — number of fills
        buys, sells    — side breakdown
        total_qty      — absolute share count summed
        total_notional — sum of |net_amount|
        first_ts, last_ts — bounds of the window for this symbol
    """
    rows = await get_recent_fills(minutes=minutes, limit=10_000)
    by_sym: dict[str, dict] = defaultdict(lambda: {
        "count": 0, "buys": 0, "sells": 0,
        "total_qty": 0.0, "total_notional": 0.0,
        "first_ts": None, "last_ts": None,
    })
    for r in rows:
        s = r.get("symbol")
        if not s:
            continue
        agg = by_sym[s]
        agg["count"] += 1
        side = (r.get("side") or "").upper()
        if side == "BUY":
            agg["buys"] += 1
        elif side == "SELL":
            agg["sells"] += 1
        agg["total_qty"] += abs(float(r.get("qty") or 0))
        agg["total_notional"] += abs(float(r.get("net_amount") or 0))
        ts = r.get("timestamp")
        if ts:
            if agg["first_ts"] is None or ts < agg["first_ts"]:
                agg["first_ts"] = ts
            if agg["last_ts"] is None or ts > agg["last_ts"]:
                agg["last_ts"] = ts

    # Sort: most-active symbol first.
    sorted_out = sorted(
        ({"symbol": k, **v} for k, v in by_sym.items()),
        key=lambda r: r["count"], reverse=True,
    )
    return {
        "window_minutes": minutes,
        "total_fills": len(rows),
        "symbols": sorted_out,
    }


@router.get("/in-flight")
async def in_flight(
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Current in-memory in-flight order claims (pre-broker-ack lock).

    Doctrine: this is Layer B of the 2026-06-10 dedupe stack — it
    captures orders MC has submitted but Public.com has not yet
    indexed. Combined with `/summary` (Layer A: broker truth), this
    is the operator's window into "what is MC about to fire on?"
    """
    return in_flight_snapshot()
