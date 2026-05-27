"""Data Stack Phase 1 tripwires (2026-05-27).

Locks the doctrine pins for the Finnhub equity feeder, SEC EDGAR
Form-4 feeder, and FRED macro feeder. Each provider must:

  1. Carry NO execution authority. The OHLCV ingest schema rejects
     `may_execute`; the alt-data collections must also be free of it.
  2. Degrade gracefully on missing API keys or HTTP errors — failed
     fetches write to feeder_health_audit but never raise to callers.
  3. Be idempotent — re-running a fetch produces 0 net writes.

These tests use httpx.MockTransport to simulate provider responses;
no real API calls are made.
"""
from __future__ import annotations

import httpx
import pytest

from db import db
from namespaces import (
    ALT_DATA_FILINGS,
    ALT_DATA_MACRO,
    FEEDER_HEALTH_AUDIT,
    PATTERNS_UNIVERSE,
    SHARED_OHLCV_BARS,
    SYMBOL_METADATA,
)


pytestmark = [pytest.mark.tripwire, pytest.mark.asyncio]


# ──────────────────────── FEEDERS dict ────────────────────────


async def test_finnhub_equity_in_feeders_dict():
    """The new feeder must appear in `shared/technicals.py:FEEDERS`."""
    from shared.technicals import FEEDERS
    assert "finnhub_equity" in FEEDERS
    assert FEEDERS["finnhub_equity"] == "FINNHUB_FEEDER_TOKEN"


async def test_ohlcv_schema_accepts_finnhub_equity_source():
    """OHLCVBarIn.source Literal must include finnhub_equity."""
    from shared.technicals import OHLCVBarIn
    bar = OHLCVBarIn(
        source="finnhub_equity", symbol="AAPL", tf="5m",
        ts="2026-05-27T14:30:00+00:00",
        o=170.0, h=171.0, l=169.5, c=170.5, v=100_000,
    )
    assert bar.source == "finnhub_equity"


# ──────────────────────── doctrine: no may_execute in alt-data ────────────────────────


async def test_alt_data_macro_strips_may_execute():
    """FRED ingest path must strip `may_execute` defensively."""
    from shared.alt_data.fred import _persist_obs
    docs = [{
        "provider": "fred", "series_id": "TRIPWIRE_TEST",
        "date": "2099-01-01", "value": 1.23, "units": "Index",
        "may_execute": True,  # smuggled — must be stripped
    }]
    inserted = await _persist_obs(docs)
    assert inserted == 1
    row = await db[ALT_DATA_MACRO].find_one(
        {"series_id": "TRIPWIRE_TEST"}, {"_id": 0},
    )
    assert row is not None
    assert "may_execute" not in row
    await db[ALT_DATA_MACRO].delete_many({"series_id": "TRIPWIRE_TEST"})


async def test_alt_data_filings_strips_may_execute():
    """SEC EDGAR ingest path must strip `may_execute` defensively."""
    from shared.alt_data.sec_edgar import _persist_filings
    rows = [{
        "provider": "sec_edgar", "kind": "form4_index",
        "symbol": "TRIPWIRE_TEST", "cik": "0000000001",
        "accession_number": "tw-acc-1",
        "filing_date": "2099-01-01",
        "primary_document": "x.htm",
        "archive_url": "https://example.com/x.htm",
        "ingested_at": "2099-01-01T00:00:00+00:00",
        "may_execute": True,  # smuggled — must be stripped
    }]
    inserted = await _persist_filings(rows)
    assert inserted == 1
    row = await db[ALT_DATA_FILINGS].find_one(
        {"accession_number": "tw-acc-1"}, {"_id": 0},
    )
    assert row is not None
    assert "may_execute" not in row
    await db[ALT_DATA_FILINGS].delete_many({"accession_number": "tw-acc-1"})


# ──────────────────────── Finnhub fetch shape ────────────────────────


async def test_finnhub_candles_to_bars_shape():
    """Transformer must produce valid OHLCVBarIn rows from the documented
    /stock/candle response."""
    from shared.feeders.finnhub_equity import candles_to_bars
    payload = {
        "c": [171.2, 171.5], "o": [170.8, 171.3],
        "h": [171.4, 171.7], "l": [170.7, 171.0],
        "v": [100200, 150300], "t": [1704067200, 1704070800],
        "s": "ok",
    }
    bars = candles_to_bars("AAPL", "5", payload)
    assert len(bars) == 2
    assert bars[0]["source"] == "finnhub_equity"
    assert bars[0]["symbol"] == "AAPL"
    assert bars[0]["tf"] == "5m"
    assert bars[0]["o"] == 170.8
    assert bars[0]["c"] == 171.2
    assert "T" in bars[0]["ts"]  # ISO-formatted


async def test_finnhub_fetch_candles_429_records_audit(monkeypatch):
    """Provider 429 → one row in feeder_health_audit, returns None."""
    from shared.feeders import finnhub_equity

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "30"}, json={})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        finnhub_equity, "_client",
        httpx.AsyncClient(transport=transport, base_url=finnhub_equity.FINNHUB_BASE_URL),
    )
    before = await db[FEEDER_HEALTH_AUDIT].count_documents(
        {"provider": "finnhub_equity", "error_type": "rate_limit"},
    )
    out = await finnhub_equity.fetch_candles("AAPL", "5", 0, 100, "fake-key")
    assert out is None
    after = await db[FEEDER_HEALTH_AUDIT].count_documents(
        {"provider": "finnhub_equity", "error_type": "rate_limit"},
    )
    assert after == before + 1
    await finnhub_equity._close_client()


async def test_finnhub_fetch_candles_happy_path(monkeypatch):
    """200 OK with s=ok → returns the payload."""
    from shared.feeders import finnhub_equity

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "c": [10.0], "o": [9.0], "h": [11.0], "l": [8.5],
            "v": [1000], "t": [1704067200], "s": "ok",
        })

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        finnhub_equity, "_client",
        httpx.AsyncClient(transport=transport, base_url=finnhub_equity.FINNHUB_BASE_URL),
    )
    out = await finnhub_equity.fetch_candles("HOTH", "5", 0, 100, "fake-key")
    assert out is not None
    assert out["s"] == "ok"
    await finnhub_equity._close_client()


async def test_finnhub_profile_upsert_populates_metadata():
    """Profile upsert must persist float_shares_millions and
    market_cap_millions so the pattern detector can read them."""
    from shared.feeders.finnhub_equity import upsert_symbol_metadata
    profile = {
        "name": "Trip Wire Co", "exchange": "NASDAQ",
        "country": "US", "currency": "USD",
        "finnhubIndustry": "Technology",
        "ipo": "2020-01-01",
        "marketCapitalization": 250.0,
        "shareOutstanding": 12.5,
    }
    await upsert_symbol_metadata("TWCO", profile)
    row = await db[SYMBOL_METADATA].find_one(
        {"symbol": "TWCO"}, {"_id": 0},
    )
    assert row is not None
    assert row["float_shares_millions"] == 12.5
    assert row["market_cap_millions"] == 250.0
    assert row["sector"] == "Technology"
    await db[SYMBOL_METADATA].delete_many({"symbol": "TWCO"})


# ──────────────────────── FRED fetch shape ────────────────────────


async def test_fred_observations_to_docs_skips_dot_values():
    """FRED returns `.` for missing values; transformer must skip them."""
    from shared.alt_data.fred import observations_to_docs
    payload = {
        "units": "Index",
        "realtime_start": "2026-05-27",
        "realtime_end": "2026-05-27",
        "observations": [
            {"date": "2026-01-01", "value": "100.5"},
            {"date": "2026-02-01", "value": "."},        # SKIP
            {"date": "2026-03-01", "value": "101.0"},
            {"date": "2026-04-01", "value": "not-a-num"}, # SKIP
        ],
    }
    docs = observations_to_docs("TRIPWIRE_CPI", payload)
    assert len(docs) == 2
    assert docs[0]["value"] == 100.5
    assert docs[1]["date"] == "2026-03-01"


async def test_fred_persist_obs_is_idempotent():
    """Re-persisting same obs MUST upsert, not duplicate."""
    from shared.alt_data.fred import _persist_obs
    docs = [{
        "provider": "fred", "series_id": "TRIPWIRE_IDEMP",
        "date": "2099-01-01", "value": 42.0,
    }]
    inserted_first = await _persist_obs(docs)
    inserted_second = await _persist_obs(docs)
    assert inserted_first == 1
    assert inserted_second == 0  # already there
    count = await db[ALT_DATA_MACRO].count_documents(
        {"series_id": "TRIPWIRE_IDEMP"},
    )
    assert count == 1
    await db[ALT_DATA_MACRO].delete_many({"series_id": "TRIPWIRE_IDEMP"})


# ──────────────────────── EDGAR fetch shape ────────────────────────


async def test_edgar_extract_form4_filings():
    """Filings extractor isolates Form-4 entries and builds archive URLs."""
    from shared.alt_data.sec_edgar import extract_form4_filings
    submissions = {
        "filings": {"recent": {
            "form": ["4", "10-Q", "4", "8-K"],
            "accessionNumber": ["0001-24-001", "0002-24-002", "0001-24-003", "0002-24-004"],
            "filingDate": ["2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04"],
            "primaryDocument": ["f1.xml", "10q.htm", "f2.xml", "8k.htm"],
        }},
    }
    rows = extract_form4_filings(submissions, "AAPL", "0000320193")
    assert len(rows) == 2
    assert rows[0]["kind"] == "form4_index"
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["accession_number"] == "0001-24-001"
    assert "Archives" in rows[0]["archive_url"]


async def test_edgar_persist_filings_is_idempotent():
    """Re-persisting same filing MUST upsert (no duplicates)."""
    from shared.alt_data.sec_edgar import _persist_filings
    rows = [{
        "provider": "sec_edgar", "kind": "form4_index",
        "symbol": "TRIPWIRE_FILE", "cik": "0000000001",
        "accession_number": "tw-idemp-1",
        "filing_date": "2099-01-01",
        "primary_document": "x.htm",
        "archive_url": "https://example.com/x.htm",
        "ingested_at": "2099-01-01T00:00:00+00:00",
    }]
    inserted_first = await _persist_filings(rows)
    inserted_second = await _persist_filings(rows)
    assert inserted_first == 1
    assert inserted_second == 0
    count = await db[ALT_DATA_FILINGS].count_documents(
        {"accession_number": "tw-idemp-1"},
    )
    assert count == 1
    await db[ALT_DATA_FILINGS].delete_many(
        {"accession_number": "tw-idemp-1"},
    )


# ──────────────────────── workers no-op when disabled ────────────────────────


async def test_workers_noop_when_disabled(monkeypatch):
    """All three workers must short-circuit when *_ENABLED!=true."""
    from shared.feeders import finnhub_equity
    from shared.alt_data import fred, sec_edgar
    monkeypatch.delenv("FINNHUB_ENABLED", raising=False)
    monkeypatch.delenv("FRED_ENABLED", raising=False)
    monkeypatch.delenv("SEC_EDGAR_ENABLED", raising=False)
    assert finnhub_equity._read_config()["enabled"] is False
    assert fred._read_config()["enabled"] is False
    assert sec_edgar._read_config()["enabled"] is False


async def test_finnhub_poll_once_no_universe_returns_zero(monkeypatch):
    """Empty watchlist → poll cycle returns zero with no provider calls."""
    from shared.feeders import finnhub_equity
    # Stash existing universe rows so we don't disrupt prod seed.
    existing = await db[PATTERNS_UNIVERSE].find({}, {"_id": 0}).to_list(2000)
    await db[PATTERNS_UNIVERSE].delete_many({})
    try:
        summary = await finnhub_equity._poll_once("fake-key", "5")
        assert summary["symbols"] == 0
        assert summary["bars_ingested"] == 0
    finally:
        if existing:
            await db[PATTERNS_UNIVERSE].insert_many(existing)


# ──────────────────────── operator API surface ────────────────────────


async def test_universe_crud_endpoint_round_trip(auth_client, base_url):
    """POST → GET → DELETE round trip for the watchlist."""
    r = auth_client.post(
        f"{base_url}/api/admin/patterns/universe",
        json={"symbol": "TWUNIV", "note": "tripwire", "active": True},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    assert r.json()["symbol"] == "TWUNIV"

    r = auth_client.get(
        f"{base_url}/api/admin/patterns/universe", timeout=10,
    )
    assert r.status_code == 200
    symbols = {row["symbol"] for row in r.json()["items"]}
    assert "TWUNIV" in symbols

    r = auth_client.delete(
        f"{base_url}/api/admin/patterns/universe/TWUNIV?hard=true",
        timeout=10,
    )
    assert r.status_code == 200
    assert r.json()["action"] == "hard_deleted"


async def test_feeder_health_audit_endpoint_authed(auth_client, base_url):
    """Authed GET returns items + summary keys."""
    r = auth_client.get(
        f"{base_url}/api/admin/feeders/health-audit?limit=10",
        timeout=10,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body
    assert "summary" in body
    assert "doctrine" in body
