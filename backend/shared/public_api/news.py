"""Public news feed — proxy + cache + refresher.

Doctrine:
    * Upstream is base44 (CORS-open, no-auth aggregator).
    * MC caches the latest pull in `public_news_articles` so:
        1. Frontend reads via MC (single host, no third-party flakes).
        2. Brains can query the same cache for training/context.
    * Refresh cadence: 5 minutes background loop. Manual POST also OK.
    * Fail-soft: if upstream is down, serve the last known good cache.
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


logger = logging.getLogger("risedual.public_news")

router = APIRouter()

NEWS_COLLECTION = "public_news_articles"
NEWS_META_COLLECTION = "public_news_meta"
META_ID = "singleton"

UPSTREAM_URL = os.environ.get(
    "PUBLIC_NEWS_UPSTREAM_URL",
    "https://app.base44.com/api/apps/6a063102777e9125a2431bc2/functions/getFinancialNews",
)
REFRESH_INTERVAL_SEC = int(os.environ.get("PUBLIC_NEWS_REFRESH_SEC", "300"))
REFRESH_LIMIT = int(os.environ.get("PUBLIC_NEWS_REFRESH_LIMIT", "5"))

_TASK: Optional[asyncio.Task] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _fetch_upstream(limit: int = REFRESH_LIMIT) -> dict:
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=2.0)) as client:
        r = await client.post(UPSTREAM_URL, json={"limit": limit})
        r.raise_for_status()
        return r.json()


async def _persist(payload: dict) -> int:
    """Replace the news cache with the latest pull. Returns count stored."""
    articles = payload.get("articles") or []
    # Wipe + insert. Cache size is small (≤25 per refresh); replace is cheap.
    await db[NEWS_COLLECTION].delete_many({})
    if articles:
        # Stamp ingestion time + drop _id risk.
        ingest_ts = _now_iso()
        for i, a in enumerate(articles):
            a["_seq"] = i  # preserve upstream ordering
            a["ingest_ts"] = ingest_ts
        await db[NEWS_COLLECTION].insert_many(articles)
    await db[NEWS_META_COLLECTION].update_one(
        {"_id": META_ID},
        {"$set": {
            "fetched_at": payload.get("fetched_at"),
            "ingested_at": _now_iso(),
            "total": payload.get("total", len(articles)),
            "upstream_ok": True,
        }},
        upsert=True,
    )
    return len(articles)


async def refresh_news_cache(limit: int = REFRESH_LIMIT) -> dict:
    """Pull from upstream, replace cache. Fail-soft: errors persist meta only."""
    try:
        payload = await _fetch_upstream(limit)
    except Exception as e:  # noqa: BLE001
        logger.warning("public_news upstream fetch failed: %s", e)
        await db[NEWS_META_COLLECTION].update_one(
            {"_id": META_ID},
            {"$set": {
                "last_error": str(e),
                "last_error_at": _now_iso(),
                "upstream_ok": False,
            }},
            upsert=True,
        )
        return {"ok": False, "error": str(e), "stored": 0}
    stored = await _persist(payload)
    logger.info("public_news cache refreshed: %d articles", stored)
    return {"ok": True, "stored": stored, "fetched_at": payload.get("fetched_at")}


# ─────────────────────── routes ───────────────────────

@router.get("/public/news")
async def get_news(limit: int = Query(default=25, ge=1, le=100)):
    """Read the cached news. Public. No auth."""
    cursor = db[NEWS_COLLECTION].find({}, {"_id": 0}).sort("_seq", 1).limit(limit)
    items = await cursor.to_list(length=limit)
    meta = await db[NEWS_META_COLLECTION].find_one({"_id": META_ID}, {"_id": 0}) or {}
    return {
        "ok": True,
        "items": items,
        "count": len(items),
        "fetched_at": meta.get("fetched_at"),
        "ingested_at": meta.get("ingested_at"),
        "upstream_ok": meta.get("upstream_ok", True),
    }


@router.post("/public/news/refresh")
async def post_refresh(limit: int = Query(default=REFRESH_LIMIT, ge=1, le=25)):
    """Force a cache refresh. Public (the upstream is itself public)."""
    result = await refresh_news_cache(limit=limit)
    if not result["ok"]:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


# ─────────────────────── background refresher ───────────────────────

async def _loop() -> None:
    logger.info("public_news refresher started: every %ss", REFRESH_INTERVAL_SEC)
    # First fetch immediately so the cache is warm.
    try:
        await refresh_news_cache()
    except Exception as e:  # noqa: BLE001
        logger.exception("public_news initial fetch failed: %s", e)
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SEC)
        try:
            await refresh_news_cache()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("public_news refresh tick failed: %s", e)


def start_news_refresher() -> None:
    global _TASK
    if _TASK and not _TASK.done():
        return
    loop = asyncio.get_event_loop()
    _TASK = loop.create_task(_loop())


async def stop_news_refresher() -> None:
    global _TASK
    if _TASK and not _TASK.done():
        _TASK.cancel()
        try:
            await _TASK
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _TASK = None
