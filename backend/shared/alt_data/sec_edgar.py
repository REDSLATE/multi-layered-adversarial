"""SEC EDGAR — Form 4 insider transactions feeder.

Polls each watchlist symbol every 15 minutes (default) for newly filed
Form 4 documents via the public submissions API on data.sec.gov.

Auth: SEC requires a `User-Agent` header with a real name + email
(politeness rule). No API key, no signup. Rate limit: 10 req/sec per
IP — our worker stays well below by serializing with a small sleep.

Doctrine pin: alt-data is descriptive. MC stores; brains read; seat
holder acts. No execution authority is conveyed by anything written
here. The `alt_data_filings` collection MUST NOT carry any
`may_execute` field — tripwire-pinned.

Configuration (backend/.env):
  SEC_EDGAR_USER_AGENT   required; e.g. "Risedual MissionControl ops@risedual.ai"
  SEC_EDGAR_ENABLED      "true" to enable
  SEC_EDGAR_POLL_INTERVAL_SEC   default 900 (15 min)
  SEC_EDGAR_REQUEST_GAP_SEC     default 0.2  (5 req/sec, safe under 10)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from db import db
from namespaces import ALT_DATA_FILINGS, PATTERNS_UNIVERSE, SYMBOL_METADATA
from shared.feeders.feeder_health import record_feeder_health


logger = logging.getLogger(__name__)

EDGAR_BASE_URL = "https://data.sec.gov"
PROVIDER = "sec_edgar"


_client: Optional[httpx.AsyncClient] = None


def _build_user_agent() -> str:
    ua = os.environ.get("SEC_EDGAR_USER_AGENT", "").strip()
    return ua or "Risedual MissionControl ops@risedual.ai"


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=EDGAR_BASE_URL,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            headers={
                "User-Agent": _build_user_agent(),
                "Accept": "application/json",
            },
        )
    return _client


async def _close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


# ──────────────────────── symbol → CIK resolution ────────────────────────

# We store CIK alongside symbol_metadata once we discover it. The first
# resolution for a symbol uses SEC's company-tickers list. This list is
# small (~10MB JSON) and is cached in symbol_metadata thereafter.
_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_ticker_to_cik: dict[str, str] = {}
_ticker_index_loaded = False


async def _ensure_ticker_index() -> None:
    """Load SEC's company-tickers list once per process. Stores into
    the module-level cache + per-symbol rows in symbol_metadata."""
    global _ticker_index_loaded
    if _ticker_index_loaded:
        return
    # Try cache first — symbols we've already resolved.
    cached = db[SYMBOL_METADATA].find(
        {"cik": {"$exists": True}}, {"_id": 0, "symbol": 1, "cik": 1},
    )
    async for row in cached:
        if row.get("symbol") and row.get("cik"):
            _ticker_to_cik[row["symbol"].upper()] = row["cik"]
    # Then pull the SEC index. Failure → leave whatever we cached.
    try:
        resp = await _get_client().get(_COMPANY_TICKERS_URL)
        if resp.status_code == 200:
            payload = resp.json()
            for entry in payload.values():
                if not isinstance(entry, dict):
                    continue
                t = (entry.get("ticker") or "").upper()
                cik_int = entry.get("cik_str")
                if t and cik_int is not None:
                    _ticker_to_cik[t] = str(cik_int).zfill(10)
    except Exception as exc:  # noqa: BLE001
        await record_feeder_health(
            provider=PROVIDER, endpoint=_COMPANY_TICKERS_URL,
            status_code=None, error_type="request_error",
            message=str(exc)[:500],
        )
    _ticker_index_loaded = True


async def resolve_cik(symbol: str) -> Optional[str]:
    await _ensure_ticker_index()
    return _ticker_to_cik.get(symbol.upper())


# ──────────────────────── filings fetch ────────────────────────

async def fetch_submissions(cik_padded: str) -> Optional[dict[str, Any]]:
    """GET /submissions/CIK<padded>.json — list of recent filings."""
    if not re.fullmatch(r"\d{10}", cik_padded):
        return None
    try:
        resp = await _get_client().get(f"/submissions/CIK{cik_padded}.json")
    except httpx.RequestError as exc:
        await record_feeder_health(
            provider=PROVIDER, endpoint="/submissions", status_code=None,
            error_type="request_error", message=str(exc),
            context={"cik": cik_padded},
        )
        return None
    if resp.status_code == 429:
        await record_feeder_health(
            provider=PROVIDER, endpoint="/submissions", status_code=429,
            error_type="rate_limit",
            message=f"retry-after={resp.headers.get('Retry-After')}",
            context={"cik": cik_padded},
        )
        return None
    if resp.status_code >= 400:
        await record_feeder_health(
            provider=PROVIDER, endpoint="/submissions",
            status_code=resp.status_code, error_type="http_status_error",
            message=resp.text[:500], context={"cik": cik_padded},
        )
        return None
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        await record_feeder_health(
            provider=PROVIDER, endpoint="/submissions",
            status_code=resp.status_code, error_type="api_error",
            message=str(exc)[:200], context={"cik": cik_padded},
        )
        return None


def extract_form4_filings(
    submissions: dict[str, Any], symbol: str, cik: str,
) -> list[dict[str, Any]]:
    """Build one row per Form-4 entry from the submissions payload.
    Descriptive only — does NOT fetch the per-filing XML body in this
    pass; that's a Phase-2 enrichment step. The filing index row gives
    the operator + brains visibility into WHEN insiders filed."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    accs = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    primary_docs = recent.get("primaryDocument") or []
    n = min(len(forms), len(accs), len(dates), len(primary_docs))
    out: list[dict[str, Any]] = []
    for i in range(n):
        if forms[i] != "4":
            continue
        acc = accs[i]
        out.append({
            "provider": PROVIDER,
            "kind": "form4_index",
            "symbol": symbol.upper(),
            "cik": cik,
            "accession_number": acc,
            "filing_date": dates[i],
            "primary_document": primary_docs[i],
            "archive_url": (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{int(cik)}/{acc.replace('-', '')}/{primary_docs[i]}"
            ),
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        })
    return out


async def _persist_filings(rows: list[dict[str, Any]]) -> int:
    """Idempotent upsert keyed on (provider, accession_number)."""
    inserted = 0
    for row in rows:
        # Tripwire-pinned: alt-data NEVER carries execution authority.
        row.pop("may_execute", None)
        result = await db[ALT_DATA_FILINGS].update_one(
            {
                "provider": row["provider"],
                "accession_number": row["accession_number"],
            },
            {"$setOnInsert": row},
            upsert=True,
        )
        if result.upserted_id is not None:
            inserted += 1
    return inserted


# ──────────────────────── universe loop ────────────────────────

async def universe_symbols() -> list[str]:
    rows = await db[PATTERNS_UNIVERSE].find(
        {"active": {"$ne": False}}, {"_id": 0, "symbol": 1},
    ).to_list(1000)
    return [r["symbol"] for r in rows if r.get("symbol")]


_task: Optional[asyncio.Task] = None
_stop_flag = False


def _read_config() -> dict[str, Any]:
    return {
        "enabled": os.environ.get("SEC_EDGAR_ENABLED", "").lower() == "true",
        "interval": int(os.environ.get("SEC_EDGAR_POLL_INTERVAL_SEC", "900")),
        "request_gap": float(os.environ.get("SEC_EDGAR_REQUEST_GAP_SEC", "0.2")),
    }


async def _poll_once(request_gap: float) -> dict[str, Any]:
    symbols = await universe_symbols()
    if not symbols:
        return {"symbols": 0, "filings_inserted": 0}
    total_inserted = 0
    for symbol in symbols:
        cik = await resolve_cik(symbol)
        if not cik:
            await record_feeder_health(
                provider=PROVIDER, endpoint="resolve_cik",
                status_code=None, error_type="api_error",
                message="no CIK mapping found", context={"symbol": symbol},
            )
            continue
        submissions = await fetch_submissions(cik)
        if not submissions:
            await asyncio.sleep(request_gap)
            continue
        rows = extract_form4_filings(submissions, symbol, cik)
        if rows:
            total_inserted += await _persist_filings(rows)
        await asyncio.sleep(request_gap)
    return {
        "symbols": len(symbols), "filings_inserted": total_inserted,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


async def _worker_loop() -> None:
    global _stop_flag
    while not _stop_flag:
        cfg = _read_config()
        if not cfg["enabled"]:
            await asyncio.sleep(60)
            continue
        try:
            summary = await _poll_once(cfg["request_gap"])
            logger.info("sec_edgar poll: %s", summary)
        except Exception as exc:  # noqa: BLE001
            logger.warning("sec_edgar poll crashed: %s", exc)
            await record_feeder_health(
                provider=PROVIDER, endpoint="_worker_loop", status_code=None,
                error_type="worker_crash", message=str(exc)[:500],
            )
        await asyncio.sleep(cfg["interval"])


def start_worker_if_enabled() -> None:
    global _task, _stop_flag
    if _task is not None and not _task.done():
        return
    cfg = _read_config()
    if not cfg["enabled"]:
        logger.info("sec_edgar worker disabled (SEC_EDGAR_ENABLED!=true)")
        return
    _stop_flag = False
    _task = asyncio.create_task(_worker_loop(), name="sec_edgar_worker")
    logger.info("sec_edgar worker started (interval=%ss)", cfg["interval"])


async def stop_worker() -> None:
    global _task, _stop_flag
    _stop_flag = True
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None
    await _close_client()
