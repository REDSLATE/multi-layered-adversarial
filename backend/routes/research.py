"""Research Layer HTTP surface.

Read-only endpoints that let the operator inspect what Strategy Lab
sees — without giving any caller a way to execute. Routes:

    GET  /api/research/signal   — current evidence for (symbol, lane)
    GET  /api/research/backtest — last-N-bar backtest of the strategy
    GET  /api/research/health   — module loaded + bar source reachable

Bar source: `shared_ohlcv_bars` Mongo collection (same source as
`/api/public/bars`). 1h timeframe by default — strategies are built
for swing-style cadence, not tick scalping.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from shared.research.backtest import backtest_strategy
from shared.research.bar_source import load_recent_bars
from shared.research.features import build_features
from shared.research.strategy_lab import (
    STRATEGIES,
    crypto_breakdown,
    large_cap_momentum,
    score_strategies,
)


router = APIRouter(prefix="/research", tags=["research"])


_VALID_TFS = frozenset({"1m", "5m", "15m", "1h", "4h", "1d"})
_VALID_LANES = frozenset({"equity", "crypto"})


async def _load_bars(symbol: str, tf: str, limit: int) -> tuple[list[dict], Optional[str]]:
    return await load_recent_bars(symbol, tf=tf, limit=limit)


def _validate_lane_tf(lane: str, tf: str) -> None:
    if lane not in _VALID_LANES:
        raise HTTPException(
            status_code=422,
            detail=f"lane must be one of {sorted(_VALID_LANES)}",
        )
    if tf not in _VALID_TFS:
        raise HTTPException(
            status_code=422,
            detail=f"tf must be one of {sorted(_VALID_TFS)}",
        )


@router.get("/health")
async def research_health(
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> dict:
    """Smoke check — module imports + reachable bar source distinct
    list. Cheap enough to be polled by the frontend on panel mount.
    """
    from db import db
    from namespaces import SHARED_OHLCV_BARS
    try:
        any_source = await db[SHARED_OHLCV_BARS].find_one(
            {}, {"_id": 0, "source": 1},
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    return {
        "ok": True,
        "strategies": {
            lane: [fn.__name__ for fn in funcs]
            for lane, funcs in STRATEGIES.items()
        },
        "bar_source_reachable": bool(any_source),
    }


@router.get("/signal")
async def research_signal(
    symbol: str = Query(..., description="ticker or pair (e.g. AAPL, BTC/USD)"),
    lane: str = Query(..., description="equity | crypto"),
    tf: str = Query("1h", description="bar timeframe"),
    limit: int = Query(120, ge=50, le=500, description="bars to inspect"),
    spread_bps: Optional[float] = Query(
        None, description="optional live spread override (bps)",
    ),
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> dict:
    """Current Strategy Lab evidence for (symbol, lane).

    Returns the latest `MarketFeatureFrame` + every applicable
    strategy's `StrategySignal`. Never executes anything; this is the
    read-only inspection endpoint the operator hits to see "what is
    the brain looking at right now?".
    """
    lane = lane.lower()
    _validate_lane_tf(lane, tf)
    bars, src = await _load_bars(symbol, tf, limit)
    if not bars:
        raise HTTPException(
            status_code=404,
            detail=f"no bars on file for symbol={symbol!r} tf={tf!r}",
        )
    features = build_features(symbol, lane, bars, spread_bps=spread_bps)
    signals = score_strategies(features)
    return {
        "symbol": symbol,
        "lane": lane,
        "tf": tf,
        "source": src,
        "bars_used": len(bars),
        "features": features.__dict__,
        "signals": [
            {
                "strategy_id": s.strategy_id,
                "direction": s.direction,
                "score": s.score,
                "confidence": s.confidence,
                "reasons": s.reasons,
            }
            for s in signals
        ],
    }


@router.get("/backtest")
async def research_backtest(
    symbol: str = Query(...),
    lane: str = Query(...),
    strategy_id: Optional[str] = Query(
        None,
        description="strategy_id (default = lane's first registered strategy)",
    ),
    tf: str = Query("1h"),
    limit: int = Query(300, ge=100, le=1000),
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> dict:
    """Bar-by-bar backtest of the requested strategy over the last
    `limit` bars. Returns hit-rate + average forward-return-bps, never
    a P&L number — Strategy Lab can score, it can't trade.
    """
    lane = lane.lower()
    _validate_lane_tf(lane, tf)

    # Resolve the strategy callable. Default is the first registered
    # strategy for the lane (today: one per lane).
    by_id = {
        "large_cap_momentum_v1": large_cap_momentum,
        "crypto_breakdown_v1":   crypto_breakdown,
    }
    if strategy_id:
        fn = by_id.get(strategy_id)
        if fn is None:
            raise HTTPException(
                status_code=422,
                detail=f"unknown strategy_id; known: {sorted(by_id)}",
            )
    else:
        lane_funcs = STRATEGIES.get(lane) or []
        if not lane_funcs:
            raise HTTPException(
                status_code=422,
                detail=f"no strategies registered for lane={lane!r}",
            )
        fn = lane_funcs[0]

    bars, src = await _load_bars(symbol, tf, limit)
    if not bars:
        raise HTTPException(
            status_code=404,
            detail=f"no bars on file for symbol={symbol!r} tf={tf!r}",
        )
    result = backtest_strategy(bars, fn, symbol, lane)
    result["symbol"] = symbol
    result["lane"] = lane
    result["tf"] = tf
    result["source"] = src
    return result
