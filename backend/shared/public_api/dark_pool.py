"""Public dark-pool / congressional / insider feed — direct upstream.

Doctrine: same as `news.py` — no third-party paid keys. Pull from:
    * Congressional: House Stock Watcher + Senate Stock Watcher (free APIs)
    * Insider trades: SEC EDGAR Form 4 filings (free, polite User-Agent)
    * Corporate filings: SEC EDGAR 13F/13D/13G atom feed (free)

Cache → 5-minute background refresh → frontend + brain context.
"""
from __future__ import annotations

import asyncio
import logging
import os
import xml.etree.ElementTree as ET
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

REFRESH_INTERVAL_SEC = int(os.environ.get("PUBLIC_DARKPOOL_REFRESH_SEC", "300"))
PER_TYPE_LIMIT = int(os.environ.get("PUBLIC_DARKPOOL_LIMIT", "50"))
# SEC demands a real contact in User-Agent or they 403. Operator may
# override via env if they want their own contact in the string.
USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "RISEDUAL-MissionControl contact@risedual.ai (research aggregator)",
)

HOUSE_URL = "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json"
SENATE_URL = "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json"
EDGAR_FORM4_ATOM = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&owner=include&count=40&output=atom"
EDGAR_13F_ATOM = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=13F&owner=include&count=40&output=atom"


_TASK: Optional[asyncio.Task] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


# ─────────────────────── congressional ───────────────────────

def _normalize_congress_row(row: dict, chamber: str) -> dict:
    """Pull the few fields we care about from a raw stockwatcher row."""
    return {
        "chamber": chamber,
        "representative": row.get("representative") or row.get("senator"),
        "party": row.get("party"),
        "ticker": (row.get("ticker") or "").upper().strip(),
        "asset_description": row.get("asset_description") or row.get("asset_name"),
        "transaction": row.get("type") or row.get("transaction_type"),
        "amount": row.get("amount"),
        "transaction_date": row.get("transaction_date") or row.get("date"),
        "disclosure_date": row.get("disclosure_date"),
        "owner": row.get("owner"),
    }


async def _fetch_congressional(client: httpx.AsyncClient, limit: int) -> list[dict]:
    items: list[dict] = []
    for url, chamber in ((HOUSE_URL, "house"), (SENATE_URL, "senate")):
        try:
            r = await client.get(url, headers={"User-Agent": USER_AGENT})
            r.raise_for_status()
            data = r.json()
            # Both feeds return a JSON array of trades.
            if isinstance(data, list):
                rows = data[-limit:][::-1]  # most recent N, newest first
                for row in rows:
                    items.append(_normalize_congress_row(row, chamber))
        except Exception as e:  # noqa: BLE001
            logger.info("congressional fetch failed for %s: %s", chamber, e)
            continue
    # Sort newest-first by transaction_date when available.
    items.sort(key=lambda r: r.get("transaction_date") or "", reverse=True)
    return items[:limit]


# ─────────────────────── EDGAR (insider + corporate) ───────────────────────

def _parse_edgar_atom(xml_bytes: bytes, kind: str, limit: int) -> list[dict]:
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.warning("EDGAR %s atom parse error: %s", kind, e)
        return items

    entries = root.findall("{http://www.w3.org/2005/Atom}entry")
    for e in entries[:limit]:
        rec: dict = {"kind": kind}
        for child in e:
            tag = _strip_ns(child.tag).lower()
            text = (child.text or "").strip()
            if tag == "title":
                rec["title"] = text
                # Title format: "FORM TYPE - FILER NAME (CIK)"
                if " - " in text:
                    _, _, after = text.partition(" - ")
                    rec["filer"] = after.split("(")[0].strip()
            elif tag == "updated":
                rec["filed_at"] = text
            elif tag == "link":
                href = child.attrib.get("href")
                if href:
                    rec["link"] = href
            elif tag == "summary":
                rec["summary"] = text[:600]
            elif tag == "category":
                term = child.attrib.get("term")
                if term:
                    rec["form_type"] = term
        # Try to extract a ticker from filer or summary; EDGAR atom doesn't
        # ship a clean ticker field. Brains will tolerate missing tickers.
        rec.setdefault("ticker", "")
        items.append(rec)
    return items


async def _fetch_edgar(client: httpx.AsyncClient, url: str, kind: str, limit: int) -> list[dict]:
    try:
        r = await client.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml"})
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        logger.info("EDGAR %s fetch failed: %s", kind, e)
        return []
    return _parse_edgar_atom(r.content, kind, limit)


# ─────────────────────── orchestrator ───────────────────────

async def _fetch_upstream(limit: int = PER_TYPE_LIMIT) -> dict:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=4.0, read=20.0, write=5.0, pool=2.0),
        follow_redirects=True,
    ) as client:
        cong, ins, corp = await asyncio.gather(
            _fetch_congressional(client, limit),
            _fetch_edgar(client, EDGAR_FORM4_ATOM, "insider_form4", limit),
            _fetch_edgar(client, EDGAR_13F_ATOM, "corporate_13f", limit),
            return_exceptions=False,
        )
    return {
        "ok": True,
        "fetched_at": _now_iso(),
        "congressional": cong,
        "insider": ins,
        "corporate": corp,
    }


async def _persist(payload: dict) -> dict:
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
            for i, it in enumerate(items):
                it["_seq"] = i
                it["ingest_ts"] = ingest_ts
            await db[coll].insert_many(items)
        counts[key] = len(items)

    await db[META_COLLECTION].update_one(
        {"_id": META_ID},
        {"$set": {
            "fetched_at": payload.get("fetched_at"),
            "ingested_at": ingest_ts,
            "counts": counts,
            "upstream_ok": sum(counts.values()) > 0,
        }},
        upsert=True,
    )
    return counts


async def refresh_darkpool_cache(limit: int = PER_TYPE_LIMIT) -> dict:
    try:
        payload = await _fetch_upstream(limit=limit)
    except Exception as e:  # noqa: BLE001
        logger.warning("public_darkpool refresh failed wholesale: %s", e)
        await db[META_COLLECTION].update_one(
            {"_id": META_ID},
            {"$set": {"last_error": str(e), "last_error_at": _now_iso(), "upstream_ok": False}},
            upsert=True,
        )
        return {"ok": False, "error": str(e), "counts": {}}
    counts = await _persist(payload)
    logger.info("public_darkpool refreshed: %s", counts)
    return {"ok": True, "counts": counts, "fetched_at": payload.get("fetched_at")}


# ─────────────────────── routes ───────────────────────

@router.get("/public/dark-pool")
async def get_darkpool(
    type: str = Query(default="all", regex="^(all|congressional|insider|corporate)$"),
    ticker: Optional[str] = Query(default=None),
    limit: int = Query(default=25, ge=1, le=200),
):
    proj = {"_id": 0}
    q: dict = {}
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
        "counts": meta.get("counts", {}),
        "upstream_ok": meta.get("upstream_ok", True),
        **result,
    }


@router.post("/public/dark-pool/refresh")
async def post_refresh(limit: int = Query(default=PER_TYPE_LIMIT, ge=1, le=200)):
    result = await refresh_darkpool_cache(limit=limit)
    if not result["ok"]:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@router.get("/brain/context/dark-pool")
async def brain_darkpool_context(
    ticker: str = Query(..., min_length=1, max_length=12),
    lookback: int = Query(default=25, ge=1, le=100),
):
    """Brain-facing aggregate. Returns counts + net direction for a ticker."""
    t = ticker.upper()
    proj = {"_id": 0}

    cong = await db[CONGRESSIONAL_COLLECTION].find({"ticker": t}, proj).sort("_seq", 1).limit(lookback).to_list(lookback)
    ins = await db[INSIDER_COLLECTION].find({"ticker": t}, proj).sort("_seq", 1).limit(lookback).to_list(lookback)
    corp = await db[CORPORATE_COLLECTION].find({"ticker": t}, proj).sort("_seq", 1).limit(lookback).to_list(lookback)

    def _direction_count(rows):
        buys = 0
        sells = 0
        for r in rows:
            v = str(r.get("transaction", "")).upper()
            if "BUY" in v or "PURCHASE" in v or "ACQUIR" in v:
                buys += 1
            elif "SELL" in v or "DISPOS" in v:
                sells += 1
        return buys, sells

    cong_buy, cong_sell = _direction_count(cong)
    ins_buy, ins_sell = _direction_count(ins)

    meta = await db[META_COLLECTION].find_one({"_id": META_ID}, {"_id": 0}) or {}
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
        "corporate": {"total": len(corp), "items": corp},
        "fetched_at": meta.get("fetched_at"),
    }


# ─────────────────────── refresher ───────────────────────

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
    _TASK = asyncio.get_event_loop().create_task(_loop())


async def stop_darkpool_refresher() -> None:
    global _TASK
    if _TASK and not _TASK.done():
        _TASK.cancel()
        try:
            await _TASK
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _TASK = None
