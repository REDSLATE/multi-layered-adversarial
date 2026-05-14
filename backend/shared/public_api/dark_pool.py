"""Public dark-pool / congressional / insider feed — proxy + cache + refresher.

Doctrine matches `news.py`:
    * Upstream base44, CORS-open, no-auth.
    * MC caches three categories in parallel collections:
        - public_congressional_trades
        - public_insider_trades
        - public_corporate_filings
    * Each refresh wipes + replaces. Brains query the same cache via
      `/api/brain/context/dark-pool` for decision-time context.
    * Fail-soft: serve last known good on upstream errors.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query

from db import db


logger = logging.getLogger("risedual.public_darkpool")

router = APIRouter()

CONGRESSIONAL_COLLECTION = "public_congressional_trades"
INSIDER_COLLECTION = "public_insider_trades"
CORPORATE_COLLECTION = "public_corporate_filings"
META_COLLECTION = "public_darkpool_meta"
META_ID = "singleton"

UPSTREAM_URL = os.environ.get(
    "PUBLIC_DARKPOOL_UPSTREAM_URL",
    "https://app.base44.com/api/apps/6a063102777e9125a2431bc2/functions/getDarkPoolData",
)
REFRESH_INTERVAL_SEC = int(os.environ.get("PUBLIC_DARKPOOL_REFRESH_SEC", "300"))
REFRESH_LIMIT = int(os.environ.get("PUBLIC_DARKPOOL_REFRESH_LIMIT", "50"))

_TASK: Optional[asyncio.Task] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _fetch_upstream(limit: int = REFRESH_LIMIT, type_: str = "all") -> dict:
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=3.0, read=15.0, write=5.0, pool=2.0)) as client_:
        r = await client_.post(UPSTREAM_URL, json={"type": type_, "limit": limit})
        r.raise_for_status()
        return r.json()


async def _persist(payload: dict) -> dict:
    """Replace each category in the cache. Returns count per category."""
    ingest_ts = _now_iso()
    counts = {}

    for key, coll in (
        ("congressional", CONGRESSIONAL_COLLECTION),
        ("insider", INSIDER_COLLECTION),
        ("corporate", CORPORATE_COLLECTION),
    ):
        items = payload.get(key) or []
        await db[coll].delete_many({})
        if items:
            for i, item in enumerate(items):
                item["_seq"] = i
                item["ingest_ts"] = ingest_ts
            await db[coll].insert_many(items)
        counts[key] = len(items)

    await db[META_COLLECTION].update_one(
        {"_id": META_ID},
        {"$set": {
            "fetched_at": payload.get("fetched_at"),
            "ingested_at": ingest_ts,
            "counts": counts,
            "upstream_ok": True,
        }},
        upsert=True,
    )
    return counts


async def refresh_darkpool_cache(limit: int = REFRESH_LIMIT) -> dict:
    """Pull all three categories. Fail-soft on upstream errors."""
    try:
        payload = await _fetch_upstream(limit=limit, type_="all")
    except Exception as e:  # noqa: BLE001
        logger.warning("public_darkpool upstream fetch failed: %s", e)
        await db[META_COLLECTION].update_one(
            {"_id": META_ID},
            {"$set": {
                "last_error": str(e),
                "last_error_at": _now_iso(),
                "upstream_ok": False,
            }},
            upsert=True,
        )
        return {"ok": False, "error": str(e), "counts": {}}
    counts = await _persist(payload)
    logger.info("public_darkpool cache refreshed: %s", counts)
    return {"ok": True, "counts": counts, "fetched_at": payload.get("fetched_at")}


# ─────────────────────── public site routes ───────────────────────

@router.get("/public/dark-pool")
async def get_darkpool(
    type: str = Query(default="all", regex="^(all|congressional|insider|corporate)$"),
    ticker: Optional[str] = Query(default=None),
    limit: int = Query(default=25, ge=1, le=200),
):
    """Read the cached dark-pool dataset. Public. No auth."""
    proj = {"_id": 0}
    q = {}
    if ticker:
        q["ticker"] = ticker.upper()

    result: dict = {}
    meta = await db[META_COLLECTION].find_one({"_id": META_ID}, {"_id": 0}) or {}

    if type in ("all", "congressional"):
        result["congressional"] = await db[CONGRESSIONAL_COLLECTION].find(q, proj).sort("_seq", 1).limit(limit).to_list(length=limit)
    if type in ("all", "insider"):
        result["insider"] = await db[INSIDER_COLLECTION].find(q, proj).sort("_seq", 1).limit(limit).to_list(length=limit)
    if type in ("all", "corporate"):
        result["corporate"] = await db[CORPORATE_COLLECTION].find(q, proj).sort("_seq", 1).limit(limit).to_list(length=limit)

    return {
        "ok": True,
        "type": type,
        "ticker": ticker.upper() if ticker else None,
        "fetched_at": meta.get("fetched_at"),
        "ingested_at": meta.get("ingested_at"),
        "upstream_ok": meta.get("upstream_ok", True),
        **result,
    }


@router.post("/public/dark-pool/refresh")
async def post_refresh(limit: int = Query(default=REFRESH_LIMIT, ge=1, le=200)):
    """Force a cache refresh."""
    result = await refresh_darkpool_cache(limit=limit)
    if not result["ok"]:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


# ─────────────────── brain-facing context route ───────────────────
# Brains call this at decision time to factor congressional + insider
# pressure into their stance on a specific ticker.

@router.get("/brain/context/dark-pool")
async def brain_darkpool_context(
    ticker: str = Query(..., min_length=1, max_length=12),
    lookback: int = Query(default=25, ge=1, le=100),
):
    """Return aggregated insider/congressional signal for a ticker.

    Designed for brain ingestion: returns counts + net direction so a
    decision engine can apply a simple bias without parsing full filings.
    """
    t = ticker.upper()
    proj = {"_id": 0}

    cong = await db[CONGRESSIONAL_COLLECTION].find({"ticker": t}, proj).sort("_seq", 1).limit(lookback).to_list(lookback)
    ins = await db[INSIDER_COLLECTION].find({"ticker": t}, proj).sort("_seq", 1).limit(lookback).to_list(lookback)
    corp = await db[CORPORATE_COLLECTION].find({"ticker": t}, proj).sort("_seq", 1).limit(lookback).to_list(lookback)

    def _direction_count(rows, side_keys=("transaction", "type", "side", "action")):
        buys = 0
        sells = 0
        for r in rows:
            v = ""
            for k in side_keys:
                if k in r and r[k]:
                    v = str(r[k]).upper()
                    break
            if "BUY" in v or "PURCHASE" in v or "ACQUIR" in v:
                buys += 1
            elif "SELL" in v or "DISPOS" in v:
                sells += 1
        return buys, sells

    cong_buy, cong_sell = _direction_count(cong)
    ins_buy, ins_sell = _direction_count(ins)

    return {
        "ok": True,
        "ticker": t,
        "lookback": lookback,
        "congressional": {
            "total": len(cong),
            "buys": cong_buy,
            "sells": cong_sell,
            "net": cong_buy - cong_sell,
            "items": cong,
        },
        "insider": {
            "total": len(ins),
            "buys": ins_buy,
            "sells": ins_sell,
            "net": ins_buy - ins_sell,
            "items": ins,
        },
        "corporate": {
            "total": len(corp),
            "items": corp,
        },
        "fetched_at": (await db[META_COLLECTION].find_one({"_id": META_ID}) or {}).get("fetched_at"),
    }


# ─────────────────────── background refresher ───────────────────────

async def _loop() -> None:
    logger.info("public_darkpool refresher started: every %ss", REFRESH_INTERVAL_SEC)
    try:
        await refresh_darkpool_cache()
    except Exception as e:  # noqa: BLE001
        logger.exception("public_darkpool initial fetch failed: %s", e)
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SEC)
        try:
            await refresh_darkpool_cache()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("public_darkpool refresh tick failed: %s", e)


def start_darkpool_refresher() -> None:
    global _TASK
    if _TASK and not _TASK.done():
        return
    loop = asyncio.get_event_loop()
    _TASK = loop.create_task(_loop())


async def stop_darkpool_refresher() -> None:
    global _TASK
    if _TASK and not _TASK.done():
        _TASK.cancel()
        try:
            await _TASK
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _TASK = None
