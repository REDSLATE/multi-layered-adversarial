"""Kraken pair auto-sync + affordability filter (2026-07-22).

Operator finding: "Most of the intents are being blocked because of
the ticker pairs not being found." Root cause: Kraken lists ~695
USD-quoted pairs but the resolver knew ~36 (static + manual
overrides) — every universe mover outside that set was rejected at
ingest DESPITE being tradable on the very exchange the universe was
built from.

Fix, two parts, both at universe-refresh time:
  1. AUTO-MAP — any crypto universe symbol lacking a mapping is
     looked up in Kraken's public AssetPairs; USD-quoted matches are
     upserted into `kraken_pair_overrides` (source=auto_sync), the
     same store the Pair Map Editor writes and the ingest guard
     reads. Not-on-Kraken symbols stay unmapped (guard still blocks
     them — correctly).
  2. AFFORDABILITY — each pair's `ordermin` (BASE units) × current
     price = the true minimum order in USD. Pairs whose minimum
     exceeds the per-order cap are dropped from the universe with
     reason `unaffordable_ordermin` so brains stop emitting intents
     that can only die as REJECTED_CAP_EXCEEDED. Operator pins are
     exempt (explicit choice is respected; the digest will show why
     fills fail).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from db import db

logger = logging.getLogger("risedual.kraken_pair_sync")

ASSET_PAIRS_URL = "https://api.kraken.com/0/public/AssetPairs"
OVERRIDES = "kraken_pair_overrides"
_CACHE_TTL_S = 900.0  # 15 min — matches the refresh cadence

_cache: dict = {"at": 0.0, "pairs": None}

# Kraken legacy base-code aliases (wsname base → our canonical base).
_BASE_ALIASES = {"XBT": "BTC", "XDG": "DOGE"}


async def get_usd_pairs(force: bool = False) -> Optional[dict]:
    """base symbol → {pair, ordermin}. None = fetch failed (callers
    must fail-soft, never treat as 'no pairs')."""
    now = time.monotonic()
    if not force and _cache["pairs"] is not None and (now - _cache["at"]) < _CACHE_TTL_S:
        return _cache["pairs"]
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(ASSET_PAIRS_URL)
            r.raise_for_status()
            body = r.json()
        if body.get("error"):
            raise RuntimeError(str(body["error"]))
        out: dict[str, dict] = {}
        for pair_key, v in (body.get("result") or {}).items():
            # Operator spec: online markets only, no dark pools.
            if str(v.get("status") or "online").lower() != "online":
                continue
            altname = str(v.get("altname") or "")
            if altname.endswith(".d") or pair_key.endswith(".d"):
                continue
            ws = v.get("wsname") or ""
            if not ws.endswith("/USD"):
                continue
            base = ws.split("/")[0].upper()
            base = _BASE_ALIASES.get(base, base)
            out[base] = {
                "pair": pair_key,
                "ordermin": float(v.get("ordermin") or 0),
            }
        _cache.update(at=now, pairs=out)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("kraken AssetPairs fetch failed: %s", exc)
        return _cache["pairs"]  # possibly stale, possibly None


async def auto_map_symbols(symbols: list[str]) -> dict:
    """Upsert overrides for unmapped `BASE/USD` symbols that Kraken
    actually trades. Returns {mapped: [...], not_on_kraken: [...]}."""
    from shared.broker_symbol_resolver import (  # noqa: WPS433
        ensure_kraken_overrides_fresh, has_kraken_mapping,
    )
    await ensure_kraken_overrides_fresh()
    missing = []
    for s in symbols:
        base = (s or "").split("/")[0].upper()
        if base and not has_kraken_mapping(f"CRYPTO:{base}-USD"):
            missing.append((s, base))
    if not missing:
        return {"mapped": [], "not_on_kraken": []}

    pairs = await get_usd_pairs()
    if pairs is None:
        return {"mapped": [], "not_on_kraken": [], "error": "assetpairs_unavailable"}

    mapped, absent = [], []
    now_iso = datetime.now(timezone.utc).isoformat()
    for symbol, base in missing:
        hit = pairs.get(base)
        if not hit:
            absent.append(symbol)
            continue
        await db[OVERRIDES].update_one(
            {"_id": f"CRYPTO:{base}-USD"},
            {"$set": {
                "symbol": f"{base}/USD",
                "kraken_pair": hit["pair"],
                "ordermin": hit["ordermin"],
                "source": "auto_sync",
                "ts": now_iso,
            }},
            upsert=True,
        )
        mapped.append(symbol)
    if mapped:
        await ensure_kraken_overrides_fresh(force=True)
        logger.info(
            "kraken auto-sync mapped %d pairs: %s",
            len(mapped), ", ".join(mapped[:12]),
        )
    return {"mapped": mapped, "not_on_kraken": absent}


async def filter_affordable(
    rows: list[dict], per_order_cap: float,
) -> tuple[list[dict], list[dict]]:
    """Drop rows whose Kraken minimum order (ordermin × price)
    exceeds the per-order cap. Pins exempt. Fail-soft: unknown
    ordermin or missing price → keep (guard downstream)."""
    pairs = await get_usd_pairs()
    if pairs is None:
        return rows, []
    kept, dropped = [], []
    for r in rows:
        if r.get("pinned"):
            kept.append(r)
            continue
        base = (r.get("canonical_symbol") or "").split("/")[0].upper()
        price = float(r.get("price") or 0)
        info = pairs.get(base)
        if not info or price <= 0 or info["ordermin"] <= 0:
            kept.append(r)
            continue
        min_notional = info["ordermin"] * price
        if min_notional > per_order_cap:
            r["_drop_reason"] = "unaffordable_ordermin"
            r["_min_notional_usd"] = round(min_notional, 2)
            dropped.append(r)
        else:
            kept.append(r)
    if dropped:
        logger.info(
            "kraken affordability filter dropped %d pairs (min order > "
            "$%.2f cap): %s", len(dropped), per_order_cap,
            ", ".join(
                f"{d['canonical_symbol']}(${d['_min_notional_usd']})"
                for d in dropped[:10]
            ),
        )
    return kept, dropped
