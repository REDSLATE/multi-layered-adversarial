"""Kraken pair-floor auto-seeder (2026-02-17).

Doctrine (operator-pinned 2026-02-17):

    Kraken official `ordermin` × live mid = computed floor.
    Operator-set explicit floor ALWAYS wins.
    Auto-seed only missing pairs (i.e., where `operator_override=false`
    or the doc doesn't exist).
    Cache + refresh periodically.
    NEVER block trading if Kraken API fails.

Every seeded row carries source metadata so the operator can audit
where a given floor came from:

    {
        "_id":              "ETH/USD",
        "min_notional_usd":  5.12,
        "policy":            "size_up",
        "source":            "kraken_auto_seed",
        "ordermin":          "0.001",
        "mid_price":         5120.00,
        "kraken_pair_code":  "XETHZUSD",
        "updated_at":        iso8601,
        "operator_override": false,
    }

Operator-touched rows carry `operator_override=true` and are NEVER
overwritten by this seeder. The `routes/kraken_pair_floors.py` PUT
handler stamps that field on every operator write.

Wire mapping: Kraken returns pair codes like `XETHZUSD` and wsnames
like `XBT/USD`. We use `wsname` as the canonical form, with a single
substitution: `XBT` → `BTC` (Kraken's legacy code for Bitcoin, our
canon).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

from db import db
from shared.kraken_pair_floors import (
    COLLECTION as PAIR_FLOORS_COLLECTION,
    invalidate_cache,
)


logger = logging.getLogger("kraken_auto_seed")

_ASSET_PAIRS_URL = "https://api.kraken.com/0/public/AssetPairs"
_TICKER_URL = "https://api.kraken.com/0/public/Ticker"
_HTTP_TIMEOUT = 8.0
_DEFAULT_REFRESH_INTERVAL_S = int(
    os.environ.get("KRAKEN_AUTO_SEED_REFRESH_S", "900")  # 15 min
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonicalize_wsname(wsname: str) -> str:
    """Kraken's `wsname` uses XBT for Bitcoin. Our canon is BTC.
    Everything else passes through unchanged."""
    if not wsname:
        return ""
    return wsname.replace("XBT/", "BTC/", 1)


async def _fetch_asset_pairs() -> dict[str, dict]:
    """Kraken → `{kraken_code → {wsname, altname, ordermin, ...}}`.
    Returns {} on any failure (doctrine: never block trading on
    Kraken API errors)."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
            r = await c.get(_ASSET_PAIRS_URL)
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("kraken_auto_seed: AssetPairs fetch failed: %s", e)
        return {}
    if data.get("error"):
        logger.warning("kraken_auto_seed: AssetPairs API error: %s", data["error"])
        return {}
    return data.get("result") or {}


async def _fetch_tickers(kraken_codes: list[str]) -> dict[str, float]:
    """Batch-fetch mid prices → `{kraken_code → mid}`. Kraken accepts
    comma-separated codes on `/Ticker?pair=`. Returns {} on failure."""
    if not kraken_codes:
        return {}
    # Kraken caps `pair=` at some size; chunk into groups of ~50 to be safe.
    out: dict[str, float] = {}
    for i in range(0, len(kraken_codes), 50):
        chunk = kraken_codes[i:i + 50]
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as c:
                r = await c.get(_TICKER_URL, params={"pair": ",".join(chunk)})
                r.raise_for_status()
                data = r.json()
        except Exception as e:  # noqa: BLE001
            logger.warning("kraken_auto_seed: Ticker chunk fetch failed: %s", e)
            continue
        if data.get("error"):
            logger.warning("kraken_auto_seed: Ticker error: %s", data["error"])
            continue
        for k, v in (data.get("result") or {}).items():
            try:
                bid = float(v["b"][0])
                ask = float(v["a"][0])
                out[k] = (bid + ask) / 2.0
            except (KeyError, ValueError, TypeError, IndexError):
                continue
    return out


async def run_once() -> dict:
    """Seed missing / non-overridden pair floors. Returns a summary
    dict for the on-demand endpoint response."""
    started = _now_iso()

    pairs = await _fetch_asset_pairs()
    if not pairs:
        return {"ok": False, "reason": "kraken_api_unreachable",
                "started_at": started, "seeded": 0, "skipped_operator_override": 0,
                "skipped_no_mid": 0}

    # Compute canonical wsname + ordermin, keyed by kraken_code
    per_code: dict[str, dict] = {}
    for code, meta in pairs.items():
        ws = meta.get("wsname") or ""
        canon = _canonicalize_wsname(ws)
        ordermin_raw = meta.get("ordermin")
        try:
            ordermin = float(ordermin_raw) if ordermin_raw is not None else None
        except (ValueError, TypeError):
            ordermin = None
        if not canon or ordermin is None or ordermin <= 0:
            continue
        per_code[code] = {
            "kraken_code": code,
            "pair": canon,
            "ordermin_str": str(ordermin_raw),
            "ordermin": ordermin,
        }

    # Batch-fetch mids for exactly the codes we care about
    mids = await _fetch_tickers(list(per_code.keys()))

    seeded = 0
    skipped_no_mid = 0
    skipped_operator_override = 0

    for code, row in per_code.items():
        mid = mids.get(code)
        if mid is None or mid <= 0:
            skipped_no_mid += 1
            continue

        # Check existing doc — respect operator overrides
        existing = await db[PAIR_FLOORS_COLLECTION].find_one(
            {"_id": row["pair"]}, {"operator_override": 1}
        )
        if existing and existing.get("operator_override") is True:
            skipped_operator_override += 1
            continue

        min_notional_usd = round(row["ordermin"] * mid, 4)
        doc_set = {
            "min_notional_usd": min_notional_usd,
            "policy": "size_up",           # doctrine default
            "source": "kraken_auto_seed",
            "ordermin": row["ordermin_str"],
            "mid_price": round(mid, 6),
            "kraken_pair_code": row["kraken_code"],
            "updated_at": _now_iso(),
            "operator_override": False,
        }
        await db[PAIR_FLOORS_COLLECTION].update_one(
            {"_id": row["pair"]},
            {"$set": doc_set},
            upsert=True,
        )
        seeded += 1

    invalidate_cache()  # let the auto-router see fresh floors next tick
    logger.info(
        "kraken_auto_seed complete seeded=%d skipped_op=%d skipped_no_mid=%d",
        seeded, skipped_operator_override, skipped_no_mid,
    )
    return {
        "ok": True,
        "started_at": started,
        "finished_at": _now_iso(),
        "pairs_seen": len(per_code),
        "seeded": seeded,
        "skipped_operator_override": skipped_operator_override,
        "skipped_no_mid": skipped_no_mid,
    }


async def start_background_task(interval_s: int = _DEFAULT_REFRESH_INTERVAL_S) -> asyncio.Task:
    """Return a long-running task that calls `run_once` every
    `interval_s` seconds. Exceptions are caught inside — the loop
    never dies unless the process is terminated."""

    async def _loop():
        # Small startup delay so we don't compete with the rest of
        # the boot sequence for network + Mongo.
        await asyncio.sleep(20)
        while True:
            try:
                await run_once()
            except Exception as e:  # noqa: BLE001
                logger.error("kraken_auto_seed loop crashed once: %s", e)
            await asyncio.sleep(interval_s)

    return asyncio.create_task(_loop(), name="kraken_auto_seed_loop")


__all__ = [
    "run_once",
    "start_background_task",
    "_canonicalize_wsname",
]
