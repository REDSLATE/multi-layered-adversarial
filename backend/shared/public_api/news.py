"""Public news feed — RSS aggregator with cache + 5min refresher.

Doctrine:
    * No third-party paid keys. Pulls directly from publisher RSS.
    * MC caches the merged feed in `public_news_articles` for:
        1. Frontend reads (single host, no third-party flakiness)
        2. Brain training/context access via the same cache
    * Refresh cadence: every 5 minutes background. Manual POST also OK.
    * Fail-soft: if a single source fails, the others still flow through;
      if ALL fail, last known cache is served untouched.

Sources (free, no auth):
    - CNBC Top News         https://www.cnbc.com/id/100003114/device/rss/rss.html
    - Fox Business          https://moxie.foxbusiness.com/google-publisher/markets.xml
    - MarketWatch Top       https://feeds.content.dowjones.io/public/rss/mw_topstories
    - Reuters Business      https://feeds.reuters.com/reuters/businessNews
    - Yahoo Finance         https://finance.yahoo.com/news/rssindex
"""
from __future__ import annotations

import asyncio
import logging
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query

from db import db


logger = logging.getLogger("risedual.public_news")

router = APIRouter()

NEWS_COLLECTION = "public_news_articles"
NEWS_META_COLLECTION = "public_news_meta"
META_ID = "singleton"

REFRESH_INTERVAL_SEC = int(os.environ.get("PUBLIC_NEWS_REFRESH_SEC", "300"))
PER_SOURCE_LIMIT = int(os.environ.get("PUBLIC_NEWS_PER_SOURCE", "5"))
USER_AGENT = "RISEDUAL-MissionControl/1.0 (+https://mission.risedual.ai; news-aggregator)"

# Source registry — display name + RSS URL. Adjust here only.
SOURCES = [
    ("CNBC",          "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("Fox Business",  "https://moxie.foxbusiness.com/google-publisher/markets.xml"),
    ("MarketWatch",   "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("Bloomberg",     "https://feeds.bloomberg.com/markets/news.rss"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
]

_TASK: Optional[asyncio.Task] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_pubdate(s: Optional[str]) -> Optional[str]:
    """Normalize a feed's pubDate string to ISO-8601 UTC if possible."""
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return None


def _strip_ns(tag: str) -> str:
    """RSS feeds sometimes namespace their tags; strip the prefix."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _parse_rss(xml_bytes: bytes, source_name: str, limit: int) -> list[dict]:
    """Return a list of normalized article dicts from RSS XML."""
    articles: list[dict] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.warning("RSS parse error for %s: %s", source_name, e)
        return articles

    # RSS-2 layout: <rss><channel><item>...</item></channel></rss>
    # Atom layout:  <feed><entry>...</entry></feed>
    items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")

    for item in items[:limit]:
        record = {"source": source_name}
        for child in item:
            tag = _strip_ns(child.tag).lower()
            text = (child.text or "").strip()
            if tag == "title":
                record["title"] = text
            elif tag == "link":
                # Atom links use href attribute
                href = child.attrib.get("href")
                record["link"] = href if href else text
            elif tag in ("description", "summary"):
                # Strip raw HTML aggressively — frontend renders plain text.
                cleaned = text.replace("&nbsp;", " ").strip()
                if "<" in cleaned:
                    cleaned = ET.tostring(child, encoding="unicode", method="text").strip()
                record["summary"] = cleaned[:600]
            elif tag in ("pubdate", "published", "updated"):
                record["published"] = text
                iso = _parse_pubdate(text)
                if iso:
                    record["published_iso"] = iso
        # Only keep articles with at least a title + link.
        if record.get("title") and record.get("link"):
            articles.append(record)
    return articles


async def _fetch_source(client: httpx.AsyncClient, name: str, url: str, limit: int) -> list[dict]:
    try:
        r = await client.get(url, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        logger.info("public_news fetch failed for %s: %s", name, e)
        return []
    return _parse_rss(r.content, name, limit)


async def _fetch_upstream(per_source_limit: int = PER_SOURCE_LIMIT) -> dict:
    """Fan out to every source in parallel. Failures are isolated."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=2.0),
        follow_redirects=True,
    ) as client:
        results = await asyncio.gather(
            *(_fetch_source(client, name, url, per_source_limit) for name, url in SOURCES),
            return_exceptions=False,
        )
    flat: list[dict] = []
    sources_ok: list[str] = []
    sources_fail: list[str] = []
    for (name, _), items in zip(SOURCES, results):
        if items:
            sources_ok.append(name)
            flat.extend(items)
        else:
            sources_fail.append(name)
    # Sort by parsed pubdate desc; missing dates go last.
    flat.sort(key=lambda a: a.get("published_iso") or "", reverse=True)
    return {
        "ok": True,
        "fetched_at": _now_iso(),
        "total": len(flat),
        "articles": flat,
        "sources_ok": sources_ok,
        "sources_fail": sources_fail,
    }


async def _persist(payload: dict) -> int:
    articles = payload.get("articles") or []
    await db[NEWS_COLLECTION].delete_many({})
    if articles:
        ingest_ts = _now_iso()
        for i, a in enumerate(articles):
            a["_seq"] = i
            a["ingest_ts"] = ingest_ts
        await db[NEWS_COLLECTION].insert_many(articles)
    await db[NEWS_META_COLLECTION].update_one(
        {"_id": META_ID},
        {"$set": {
            "fetched_at": payload.get("fetched_at"),
            "ingested_at": _now_iso(),
            "total": payload.get("total", len(articles)),
            "sources_ok": payload.get("sources_ok", []),
            "sources_fail": payload.get("sources_fail", []),
            "upstream_ok": len(payload.get("sources_ok", [])) > 0,
        }},
        upsert=True,
    )
    return len(articles)


async def refresh_news_cache(limit: int = PER_SOURCE_LIMIT) -> dict:
    try:
        payload = await _fetch_upstream(per_source_limit=limit)
    except Exception as e:  # noqa: BLE001
        logger.warning("public_news refresh failed wholesale: %s", e)
        await db[NEWS_META_COLLECTION].update_one(
            {"_id": META_ID},
            {"$set": {"last_error": str(e), "last_error_at": _now_iso(), "upstream_ok": False}},
            upsert=True,
        )
        return {"ok": False, "error": str(e), "stored": 0}
    stored = await _persist(payload)
    logger.info(
        "public_news refreshed: %d articles | ok=%s fail=%s",
        stored, payload.get("sources_ok"), payload.get("sources_fail"),
    )
    return {
        "ok": True,
        "stored": stored,
        "fetched_at": payload.get("fetched_at"),
        "sources_ok": payload.get("sources_ok", []),
        "sources_fail": payload.get("sources_fail", []),
    }


# ─────────────────────── routes ───────────────────────

@router.get("/public/news")
async def get_news(limit: int = Query(default=25, ge=1, le=100)):
    cursor = db[NEWS_COLLECTION].find({}, {"_id": 0}).sort("_seq", 1).limit(limit)
    items = await cursor.to_list(length=limit)
    meta = await db[NEWS_META_COLLECTION].find_one({"_id": META_ID}, {"_id": 0}) or {}
    return {
        "ok": True,
        "items": items,
        "count": len(items),
        "fetched_at": meta.get("fetched_at"),
        "ingested_at": meta.get("ingested_at"),
        "sources_ok": meta.get("sources_ok", []),
        "sources_fail": meta.get("sources_fail", []),
        "upstream_ok": meta.get("upstream_ok", True),
    }


@router.post("/public/news/refresh")
async def post_refresh(limit: int = Query(default=PER_SOURCE_LIMIT, ge=1, le=25)):
    result = await refresh_news_cache(limit=limit)
    if not result["ok"]:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


# ─────────────────────── refresher loop ───────────────────────

async def _loop() -> None:
    logger.info("public_news refresher started: every %ss across %d sources",
                REFRESH_INTERVAL_SEC, len(SOURCES))
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
    _TASK = asyncio.get_event_loop().create_task(_loop())


async def stop_news_refresher() -> None:
    global _TASK
    if _TASK and not _TASK.done():
        _TASK.cancel()
        try:
            await _TASK
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _TASK = None
