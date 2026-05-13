"""Public /bars/{symbol} — OHLCV bars for candlestick charts.

Returns the recent tail of MC's stored bars for any covered symbol.
risedual.ai's frontend renders these as candles. Source is auto-picked
(prefers kraken_pro for crypto, thinkorswim for equities). TF defaults
to 1h.

Doctrine reminders:
  * No volume detail beyond `v` — we don't expose internal book depth.
  * No future bars — only what's already ingested.
  * Tier-agnostic — bar data is the same across Free / Starter / Pro / Pro Max.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from db import db
from namespaces import SHARED_OHLCV_BARS

from .auth import PublicCaller, public_trust_required


router = APIRouter(tags=["public"])


# Preferred source per asset class — matches /heatmap's deduper.
SOURCE_PRIORITY = ["kraken_pro", "thinkorswim", "manual"]
VALID_TFS = frozenset({"1m", "5m", "15m", "1h", "4h", "1d"})


async def _pick_source(symbol: str, tf: str) -> Optional[str]:
    """Pick the best available source for (symbol, tf)."""
    sources = await db[SHARED_OHLCV_BARS].distinct(
        "source", {"symbol": symbol, "tf": tf},
    )
    if not sources:
        return None
    for s in SOURCE_PRIORITY:
        if s in sources:
            return s
    return sources[0]


@router.get("/public/bars")
async def list_covered_symbols(
    caller: PublicCaller = Depends(public_trust_required),
):
    """List symbols with at least one OHLCV bar on file.

    Used by the frontend's symbol picker. Groups by (symbol → tfs available).
    """
    pipeline = [
        {"$group": {"_id": {"symbol": "$symbol", "tf": "$tf", "source": "$source"}}},
    ]
    rows = await db[SHARED_OHLCV_BARS].aggregate(pipeline).to_list(1000)
    by_symbol: dict[str, dict] = {}
    for r in rows:
        k = r["_id"]
        sym = k["symbol"]
        by_symbol.setdefault(sym, {"symbol": sym, "tfs": set(), "sources": set()})
        by_symbol[sym]["tfs"].add(k["tf"])
        by_symbol[sym]["sources"].add(k["source"])
    items = sorted(
        ({
            "symbol": v["symbol"],
            "tfs": sorted(v["tfs"]),
            "sources": sorted(v["sources"]),
        } for v in by_symbol.values()),
        key=lambda x: x["symbol"],
    )
    return {"items": items, "count": len(items), "tier": caller.tier}


@router.get("/public/bars/{symbol:path}")
async def get_bars(
    symbol: str,
    tf: str = Query("1h", description="bar timeframe"),
    limit: int = Query(200, ge=10, le=500),
    source: Optional[str] = Query(None, description="optional source override"),
    caller: PublicCaller = Depends(public_trust_required),
):
    """Recent OHLCV bars for `symbol`. Newest last (ascending time).

    `symbol` accepts `/` so crypto pairs like `BTC/USD` route correctly
    via FastAPI's path converter.

    Response shape:
        {
          "symbol": "BTC/USD",
          "tf": "1h",
          "source": "kraken_pro",
          "bars": [{"ts","o","h","l","c","v"}, ...],   # asc by ts
          "count": N
        }
    """
    if tf not in VALID_TFS:
        raise HTTPException(
            status_code=422,
            detail=f"tf must be one of {sorted(VALID_TFS)}",
        )

    chosen = source or await _pick_source(symbol, tf)
    if not chosen:
        raise HTTPException(
            status_code=404,
            detail=f"no bars on file for symbol={symbol!r} tf={tf!r}",
        )

    rows = await db[SHARED_OHLCV_BARS].find(
        {"symbol": symbol, "tf": tf, "source": chosen},
        {"_id": 0, "ts": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
    ).sort("ts", -1).to_list(limit)
    rows.reverse()

    return {
        "symbol": symbol,
        "tf": tf,
        "source": chosen,
        "bars": rows,
        "count": len(rows),
        "tier": caller.tier,
    }
