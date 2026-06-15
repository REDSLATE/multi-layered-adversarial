"""QuiverQuant alternative-data integration.

Pulls three feeds from api.quiverquant.com and persists them to
versioned `alt_data_quiver_*` Mongo collections that brains opt-in to
consume as DESCRIPTIVE EVIDENCE only (never a hard execution gate).

Feeds:
  1. /v1/live/insiders        — corporate insider Form 4 filings (PUBLIC tier)
  2. /v1/live/congresstrading — US congressional disclosures (PUBLIC tier)
  3. /v1/historical/patentmomentum/<ticker> — patent momentum (Tier 1)

Auth:
  Bearer token via `QUIVER_API_KEY` env var. If missing, every fetch
  function logs a warning and returns []. No crash; cron tick passes.

Pattern mirrors `shared/alt_data/fred.py` + `shared/alt_data/sec_edgar.py`.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

logger = logging.getLogger("risedual.alt_data.quiver")


# ─── Config ──────────────────────────────────────────────────────────
QUIVER_BASE_URL = os.environ.get("QUIVER_BASE_URL", "https://api.quiverquant.com")
QUIVER_API_KEY  = os.environ.get("QUIVER_API_KEY", "").strip()
QUIVER_TIMEOUT_S = float(os.environ.get("QUIVER_TIMEOUT_S", "20"))

# Mongo collection names (versioned per the alt_data doctrine — schema
# changes ship as v2 collections so brains consuming v1 don't break).
COLL_INSIDER  = "alt_data_quiver_insider_v1"
COLL_CONGRESS = "alt_data_quiver_congress_v1"
COLL_PATENTS  = "alt_data_quiver_patents_v1"


def is_configured() -> bool:
    """True when QUIVER_API_KEY is set in the env."""
    return bool(QUIVER_API_KEY)


def _headers() -> dict[str, str] | None:
    if not QUIVER_API_KEY:
        return None
    return {
        "Authorization": f"Bearer {QUIVER_API_KEY}",
        "Accept": "application/json",
    }


async def _get(path: str, params: dict | None = None) -> list[dict] | None:
    """Async GET with graceful degradation. Returns None on any error
    so callers can `if rows is None: return 0`."""
    h = _headers()
    if h is None:
        logger.debug("quiver: QUIVER_API_KEY not set; skipping %s", path)
        return None
    try:
        async with httpx.AsyncClient(
            base_url=QUIVER_BASE_URL, headers=h, timeout=QUIVER_TIMEOUT_S,
        ) as client:
            r = await client.get(path, params=params or {})
        if r.status_code == 401:
            logger.error("quiver: 401 unauthorized — check QUIVER_API_KEY")
            return None
        if r.status_code == 403:
            logger.warning(
                "quiver: 403 forbidden for %s — your tier doesn't include this feed", path,
            )
            return None
        if r.status_code == 429:
            logger.warning("quiver: 429 rate-limited on %s; backing off", path)
            return None
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            logger.warning("quiver: unexpected non-list response from %s", path)
            return None
        return data
    except httpx.HTTPError as e:
        logger.warning("quiver: HTTP error on %s: %s", path, e)
        return None
    except Exception as e:  # noqa: BLE001
        logger.exception("quiver: unexpected error on %s: %s", path, e)
        return None


# ─── Fetchers ────────────────────────────────────────────────────────


async def fetch_insider_trades() -> list[dict] | None:
    """Live insider Form 4 trades. PUBLIC tier."""
    return await _get("/beta/live/insiders")


async def fetch_congress_trades() -> list[dict] | None:
    """Live congressional disclosures. PUBLIC tier."""
    return await _get("/beta/live/congresstrading")


async def fetch_patent_momentum(ticker: str) -> list[dict] | None:
    """Patent momentum for a single ticker. TIER 1 (requires paid sub).
    Returns None gracefully on 403 if tier doesn't include it."""
    return await _get(f"/beta/historical/patentmomentum/{ticker.upper()}")


# ─── Persistence ─────────────────────────────────────────────────────


def _parse_dt(value: Any) -> str | None:
    """Best-effort ISO normalization. Returns None for unparseable."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    try:
        s = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(timezone.utc).isoformat()
    except Exception:
        return str(value)  # keep raw — better than dropping


async def store_insider(db, rows: Iterable[dict]) -> int:
    """Upsert insider trade rows. Key: (Ticker, Insider, Date)."""
    n = 0
    for r in rows or []:
        ticker = r.get("Ticker") or r.get("ticker")
        insider = r.get("Insider") or r.get("InsiderName") or r.get("insider_name")
        date = _parse_dt(r.get("Date") or r.get("TransactionDate") or r.get("transaction_date"))
        if not ticker or not insider or not date:
            continue
        key = {"ticker": ticker, "insider": insider, "transaction_date": date}
        await db[COLL_INSIDER].update_one(
            key,
            {"$set": {
                **key,
                "filing_date":      _parse_dt(r.get("FilingDate") or r.get("filing_date")),
                "transaction_type": r.get("Transaction") or r.get("transaction_type"),
                "shares":           r.get("Shares") or r.get("shares"),
                "price":            r.get("Price") or r.get("price"),
                "value":            r.get("Value") or r.get("value"),
                "raw":              r,
                "ingested_at":      datetime.now(timezone.utc).isoformat(),
            }},
            upsert=True,
        )
        n += 1
    return n


async def store_congress(db, rows: Iterable[dict]) -> int:
    """Upsert congress trade rows. Key: (Ticker, Representative, TransactionDate)."""
    n = 0
    for r in rows or []:
        ticker = r.get("Ticker") or r.get("ticker")
        member = r.get("Representative") or r.get("Senator") or r.get("Member") or r.get("member_name")
        tx_date = _parse_dt(r.get("TransactionDate") or r.get("transaction_date"))
        if not ticker or not member or not tx_date:
            continue
        key = {"ticker": ticker, "member": member, "transaction_date": tx_date}
        await db[COLL_CONGRESS].update_one(
            key,
            {"$set": {
                **key,
                "filing_date":      _parse_dt(r.get("ReportDate") or r.get("filing_date")),
                "chamber":          r.get("House") or r.get("Chamber") or r.get("chamber"),
                "party":            r.get("Party") or r.get("party"),
                "transaction_type": r.get("Transaction") or r.get("transaction_type"),
                "amount_range":     r.get("Range") or r.get("Amount") or r.get("amount_range"),
                "state":            r.get("State") or r.get("state"),
                "raw":              r,
                "ingested_at":      datetime.now(timezone.utc).isoformat(),
            }},
            upsert=True,
        )
        n += 1
    return n


async def store_patents(db, ticker: str, rows: Iterable[dict]) -> int:
    """Upsert patent momentum rows. Key: (Ticker, Date)."""
    n = 0
    for r in rows or []:
        date = _parse_dt(r.get("Date") or r.get("as_of_date"))
        if not date:
            continue
        key = {"ticker": ticker.upper(), "as_of_date": date}
        await db[COLL_PATENTS].update_one(
            key,
            {"$set": {
                **key,
                "momentum":     r.get("Momentum") or r.get("momentum"),
                "patent_count": r.get("Patents") or r.get("patent_count"),
                "raw":          r,
                "ingested_at":  datetime.now(timezone.utc).isoformat(),
            }},
            upsert=True,
        )
        n += 1
    return n


# ─── Sync orchestrator (called from /api/admin/alt-data/quiver/sync) ─


async def sync_all(db, patent_tickers: list[str] | None = None) -> dict:
    """One-shot sync of all three feeds. Returns counts per feed.
    Safe to call when QUIVER_API_KEY is missing — every fetch returns
    None, every store returns 0.
    """
    if not is_configured():
        return {
            "configured": False,
            "message": "QUIVER_API_KEY not set — set it in backend/.env to enable",
            "insider_upserted": 0,
            "congress_upserted": 0,
            "patents_upserted": 0,
            "patent_tickers": [],
        }

    insider_rows  = await fetch_insider_trades()  or []
    congress_rows = await fetch_congress_trades() or []
    insider_n  = await store_insider(db, insider_rows)
    congress_n = await store_congress(db, congress_rows)

    # Patent momentum is per-ticker. If no list provided, skip.
    patents_n = 0
    tickers_done: list[str] = []
    for t in (patent_tickers or []):
        rows = await fetch_patent_momentum(t) or []
        if rows:
            patents_n += await store_patents(db, t, rows)
            tickers_done.append(t.upper())

    return {
        "configured": True,
        "insider_upserted": insider_n,
        "congress_upserted": congress_n,
        "patents_upserted": patents_n,
        "patent_tickers": tickers_done,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
