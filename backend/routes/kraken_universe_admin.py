"""Kraken Universe Loader (2026-07-22, operator spec).

Downloads EVERY Kraken pair (AssetPairs, online only, dark pools
excluded), joins 24h Ticker stats (one bulk call), groups by quote
currency, ranks by 24h notional, and publishes a browsable snapshot
to Mission Control. Snapshot persists in `kraken_universe_snapshot`
(singleton) and auto-rebuilds when older than 6h (or ?force=1).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from shared.risk.check import _per_order_cap

logger = logging.getLogger("risedual.kraken_universe")

router = APIRouter(prefix="/admin/kraken-universe", tags=["kraken-universe"])

SNAPSHOT = "kraken_universe_snapshot"
SNAP_ID = "kraken_universe"
STALE_AFTER_H = 6.0
ROWS_PER_QUOTE = 700  # covers all 645 USD pairs; snapshot stays <500KB

_QUOTE_ALIASES = {"XBT": "BTC", "ZUSD": "USD", "ZEUR": "EUR", "ZGBP": "GBP"}
_BASE_ALIASES = {"XBT": "BTC", "XDG": "DOGE"}
_USD_LIKE = {"USD", "USDT", "USDC"}


async def _fetch(url: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url)
        r.raise_for_status()
        body = r.json()
    if body.get("error"):
        raise RuntimeError(str(body["error"]))
    return body["result"]


async def build_snapshot() -> dict:
    """One AssetPairs call + one bulk Ticker call → ranked snapshot."""
    pairs = await _fetch("https://api.kraken.com/0/public/AssetPairs")
    tickers = await _fetch("https://api.kraken.com/0/public/Ticker")

    from shared.broker_symbol_resolver import (  # noqa: WPS433
        ensure_kraken_overrides_fresh, has_kraken_mapping,
    )
    await ensure_kraken_overrides_fresh()
    cap = _per_order_cap()

    rows = []
    for pair_key, meta in pairs.items():
        if str(meta.get("status") or "online").lower() != "online":
            continue
        altname = str(meta.get("altname") or "")
        if altname.endswith(".d") or pair_key.endswith(".d"):
            continue
        ws = str(meta.get("wsname") or "")
        if "/" not in ws:
            continue
        base_raw, quote_raw = ws.split("/", 1)
        base = _BASE_ALIASES.get(base_raw.upper(), base_raw.upper())
        quote = _QUOTE_ALIASES.get(quote_raw.upper(), quote_raw.upper())
        t = tickers.get(pair_key) or {}
        try:
            last = float((t.get("c") or [0])[0])
            vol24 = float((t.get("v") or [0, 0])[1])
            vwap24 = float((t.get("p") or [0, 0])[1])
        except (TypeError, ValueError, IndexError):
            last = vol24 = vwap24 = 0.0
        ordermin = float(meta.get("ordermin") or 0)
        min_order = round(ordermin * last, 4) if last > 0 else None
        usd_like = quote in _USD_LIKE
        rows.append({
            "pair": pair_key,
            "wsname": ws,
            "base": base,
            "quote": quote,
            "last": last,
            "notional_24h": round(vol24 * (vwap24 or last), 2),
            "ordermin": ordermin,
            "min_order_quote": min_order,
            "affordable": (
                bool(min_order is not None and min_order <= cap)
                if usd_like else None
            ),
            "mapped": (
                has_kraken_mapping(f"CRYPTO:{base}-USD") if quote == "USD" else None
            ),
        })

    by_quote: dict[str, list] = {}
    for r in rows:
        by_quote.setdefault(r["quote"], []).append(r)
    counts = {}
    kept_rows = []
    for quote, group in by_quote.items():
        group.sort(key=lambda x: -x["notional_24h"])
        counts[quote] = len(group)
        for i, r in enumerate(group[:ROWS_PER_QUOTE]):
            r["rank"] = i + 1
            kept_rows.append(r)

    snap = {
        "_id": SNAP_ID,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "total_online_pairs": len(rows),
        "counts_by_quote": dict(
            sorted(counts.items(), key=lambda kv: -kv[1]),
        ),
        "per_order_cap_usd": cap,
        "rows": kept_rows,
    }
    await db[SNAPSHOT].replace_one({"_id": SNAP_ID}, snap, upsert=True)
    logger.info(
        "kraken universe snapshot built: %d online pairs, quotes: %s",
        len(rows), list(snap["counts_by_quote"])[:6],
    )
    return snap


async def _get_snapshot(force: bool) -> dict:
    snap = await db[SNAPSHOT].find_one({"_id": SNAP_ID})
    stale = True
    if snap and not force:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(
                str(snap["built_at"]),
            )
            stale = age > timedelta(hours=STALE_AFTER_H)
        except ValueError:
            pass
    if snap is None or force or stale:
        try:
            snap = await build_snapshot()
        except Exception as exc:  # noqa: BLE001
            logger.warning("kraken universe rebuild failed: %s", exc)
            if snap is None:
                raise
    return snap


@router.get("")
async def kraken_universe(
    quote: str = Query("USD"),
    limit: int = Query(100, ge=1, le=300),
    q: Optional[str] = Query(None),
    force: bool = Query(False),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    snap = await _get_snapshot(force)
    quote_u = quote.upper()
    rows = [r for r in snap["rows"] if r["quote"] == quote_u]
    if q:
        needle = q.upper()
        rows = [r for r in rows if needle in r["base"] or needle in r["pair"]]
    rows.sort(key=lambda r: r["rank"])
    return {
        "built_at": snap["built_at"],
        "total_online_pairs": snap["total_online_pairs"],
        "counts_by_quote": snap["counts_by_quote"],
        "per_order_cap_usd": snap["per_order_cap_usd"],
        "quote": quote_u,
        "rows": rows[:limit],
    }
