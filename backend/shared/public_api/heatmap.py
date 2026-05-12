"""Public /heatmap + /sectors — market overview grids.

heatmap: per-symbol 24h % change. Computed on the fly from MC's
retained OHLCV bars: latest close vs the close ~24h prior on the same
feed.

sectors: sector rotation. MC doesn't track sector classifications
internally yet; we return what we have flagged as `degraded=true` so
the UI can show a placeholder while still rendering.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends

from db import db
from namespaces import SHARED_OHLCV_BARS, SHARED_INDICATOR_SNAPSHOTS

from .auth import PublicCaller, public_trust_required


router = APIRouter(tags=["public"])


def _color_band(pct: float) -> str:
    if pct >= 3.0:
        return "strong_buy"
    if pct >= 1.0:
        return "mild_buy"
    if pct > -1.0:
        return "neutral"
    if pct > -3.0:
        return "mild_sell"
    return "strong_sell"


async def _change_24h(source: str, symbol: str, tf: str) -> Optional[float]:
    """Returns 24h % change for the given (source, symbol, tf), or None
    if we don't have ~24h of coverage yet."""
    # Pull more than enough bars to span 24h regardless of tf.
    bars = await db[SHARED_OHLCV_BARS].find(
        {"source": source, "symbol": symbol, "tf": tf},
        {"_id": 0, "c": 1, "ts": 1},
    ).sort("ts", -1).to_list(50)
    if not bars:
        return None
    bars.reverse()
    last = bars[-1]
    try:
        last_ts = datetime.fromisoformat(last["ts"].replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    target = last_ts - timedelta(hours=24)
    # Walk back to the first bar ≤ target.
    prev = None
    for b in reversed(bars[:-1]):
        try:
            t = datetime.fromisoformat(b["ts"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if t <= target:
            prev = b
            break
    if not prev:
        prev = bars[0]    # use the oldest bar we have
    try:
        return (float(last["c"]) - float(prev["c"])) / float(prev["c"]) * 100
    except (ValueError, ZeroDivisionError):
        return None


@router.get("/public/heatmap")
async def get_heatmap(
    caller: PublicCaller = Depends(public_trust_required),
):
    """Per-symbol 24h % change. Aggregates across feeders, deduping by
    symbol (preferring kraken_pro for crypto, thinkorswim for equities)."""
    snaps = await db[SHARED_INDICATOR_SNAPSHOTS].find(
        {}, {"_id": 0, "source": 1, "symbol": 1, "tf": 1},
    ).to_list(500)
    if not snaps:
        return {"items": [], "count": 0, "degraded": True, "tier": caller.tier}

    # Pick one (source, tf) per symbol — prefer 1h for granularity, fall
    # back to 1d. Prefer kraken_pro over thinkorswim over manual.
    source_order = {"kraken_pro": 0, "thinkorswim": 1, "manual": 2}
    tf_order = {"1h": 0, "4h": 1, "1d": 2, "15m": 3, "5m": 4, "1m": 5}
    by_symbol: dict[str, dict] = {}
    for s in snaps:
        sym = s["symbol"]
        key = (source_order.get(s["source"], 99), tf_order.get(s["tf"], 99))
        if sym not in by_symbol or key < by_symbol[sym]["__key"]:
            by_symbol[sym] = {**s, "__key": key}

    items: list[dict] = []
    for sym, sel in by_symbol.items():
        pct = await _change_24h(sel["source"], sym, sel["tf"])
        if pct is None:
            continue
        items.append({
            "symbol": sym,
            "change_24h_pct": round(pct, 2),
            "color_band": _color_band(pct),
            "source": sel["source"],
        })

    items.sort(key=lambda r: r["change_24h_pct"], reverse=True)
    return {
        "items": items,
        "count": len(items),
        "degraded": False,
        "tier": caller.tier,
    }


# Static sector universe for the rotation view. MC doesn't actually
# track ETF prices yet, so we return the universe + a degraded flag.
# When the operator points a feeder at sector ETFs, this returns real
# data automatically (it reads from the same heatmap path).
SECTOR_ETFS = [
    {"symbol": "XLK", "name": "Technology"},
    {"symbol": "XLF", "name": "Financials"},
    {"symbol": "XLV", "name": "Healthcare"},
    {"symbol": "XLY", "name": "Consumer Discretionary"},
    {"symbol": "XLP", "name": "Consumer Staples"},
    {"symbol": "XLE", "name": "Energy"},
    {"symbol": "XLI", "name": "Industrials"},
    {"symbol": "XLU", "name": "Utilities"},
    {"symbol": "XLB", "name": "Materials"},
    {"symbol": "XLRE", "name": "Real Estate"},
    {"symbol": "XLC", "name": "Communication"},
]


@router.get("/public/sectors")
async def get_sectors(
    caller: PublicCaller = Depends(public_trust_required),
):
    items: list[dict] = []
    for s in SECTOR_ETFS:
        snap = await db[SHARED_INDICATOR_SNAPSHOTS].find_one(
            {"symbol": s["symbol"]}, {"_id": 0, "source": 1, "tf": 1},
        )
        if snap:
            pct = await _change_24h(snap["source"], s["symbol"], snap["tf"])
            if pct is None:
                continue
            items.append({
                "symbol": s["symbol"],
                "name": s["name"],
                "change_24h_pct": round(pct, 2),
                "color_band": _color_band(pct),
                "coverage": "live",
            })
        else:
            items.append({
                "symbol": s["symbol"],
                "name": s["name"],
                "change_24h_pct": None,
                "color_band": "neutral",
                "coverage": "not_wired",
            })
    live = [r for r in items if r.get("coverage") == "live"]
    best = max(live, key=lambda r: r["change_24h_pct"], default=None)
    worst = min(live, key=lambda r: r["change_24h_pct"], default=None)
    return {
        "items": items,
        "best": best,
        "worst": worst,
        "degraded": len(live) == 0,
        "tier": caller.tier,
    }
