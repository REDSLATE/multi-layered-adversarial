"""Tests for the QuiverQuant alt-data integration.

Auth-less mode (no QUIVER_API_KEY): every fetch returns None,
sync returns configured=False with zero upserts.
With a mocked SDK response: fetchers return data and stores upsert
to the right Mongo collections with proper de-dup keys.
"""
from __future__ import annotations

import os
from unittest.mock import patch, AsyncMock

import pytest

pytestmark = pytest.mark.asyncio


async def _reset():
    from db import db
    from shared.alt_data import quiver_quant as qq
    await db[qq.COLL_INSIDER].delete_many({"insider": {"$regex": "^QQTEST_"}})
    await db[qq.COLL_CONGRESS].delete_many({"member": {"$regex": "^QQTEST_"}})
    await db[qq.COLL_PATENTS].delete_many({"ticker": {"$regex": "^QQTEST_"}})


async def test_no_api_key_skips_gracefully(monkeypatch):
    """No QUIVER_API_KEY → fetchers return None, sync returns
    configured=False, no exceptions."""
    import importlib
    monkeypatch.delenv("QUIVER_API_KEY", raising=False)
    from shared.alt_data import quiver_quant as qq
    importlib.reload(qq)
    assert qq.is_configured() is False
    assert (await qq.fetch_insider_trades()) is None
    assert (await qq.fetch_congress_trades()) is None
    assert (await qq.fetch_patent_momentum("AAPL")) is None
    from db import db
    r = await qq.sync_all(db, patent_tickers=["AAPL"])
    assert r["configured"] is False
    assert r["insider_upserted"] == 0
    assert r["congress_upserted"] == 0


async def test_store_insider_upserts_and_dedups():
    """Two writes of the same row collapse via the (ticker, insider,
    transaction_date) composite key."""
    from db import db
    from shared.alt_data import quiver_quant as qq
    await _reset()
    rows = [{
        "Ticker": "QQTEST_NVDA", "Insider": "QQTEST_Jensen Huang",
        "Date": "2026-02-19T10:00:00Z", "Transaction": "P",
        "Shares": 100, "Price": 500.0, "Value": 50000.0,
    }]
    n1 = await qq.store_insider(db, rows)
    n2 = await qq.store_insider(db, rows)  # idempotent
    assert n1 == 1 and n2 == 1
    count = await db[qq.COLL_INSIDER].count_documents(
        {"insider": "QQTEST_Jensen Huang", "ticker": "QQTEST_NVDA"},
    )
    assert count == 1, "upsert must collapse duplicates"
    await _reset()


async def test_store_congress_handles_both_naming_conventions():
    """Quiver has shipped field names with both CamelCase and snake_case
    over time. Our normalizer should accept either."""
    from db import db
    from shared.alt_data import quiver_quant as qq
    await _reset()
    rows = [
        # CamelCase variant
        {"Ticker": "QQTEST_AAPL", "Representative": "QQTEST_Pelosi",
         "TransactionDate": "2026-02-15", "Transaction": "Purchase",
         "Range": "$1M – $5M", "House": "House"},
        # snake_case variant
        {"ticker": "QQTEST_TSLA", "member_name": "QQTEST_Crapo",
         "transaction_date": "2026-02-16", "transaction_type": "Sale",
         "amount_range": "$15k – $50k", "chamber": "Senate"},
    ]
    n = await qq.store_congress(db, rows)
    assert n == 2
    p = await db[qq.COLL_CONGRESS].find_one({"member": "QQTEST_Pelosi"})
    assert p["chamber"] == "House"
    c = await db[qq.COLL_CONGRESS].find_one({"member": "QQTEST_Crapo"})
    assert c["chamber"] == "Senate"
    await _reset()


async def test_store_skips_records_missing_required_fields():
    """Rows lacking ticker / insider / date should be silently dropped
    (Quiver sometimes returns partial rows when a filing is malformed)."""
    from db import db
    from shared.alt_data import quiver_quant as qq
    await _reset()
    rows = [
        {"Ticker": "QQTEST_NVDA"},  # missing Insider + Date
        {"Insider": "QQTEST_X", "Date": "2026-01-01"},  # missing Ticker
    ]
    n = await qq.store_insider(db, rows)
    assert n == 0
    await _reset()


async def test_sync_all_with_mocked_fetch():
    """End-to-end: mock the HTTP layer, prove sync calls all three
    fetchers and writes to the right collections."""
    from db import db
    from shared.alt_data import quiver_quant as qq
    await _reset()
    # Force the module to think it's configured.
    with patch.object(qq, "is_configured", return_value=True), \
         patch.object(qq, "fetch_insider_trades", new_callable=AsyncMock,
                      return_value=[{"Ticker": "QQTEST_AAPL",
                                     "Insider": "QQTEST_Cook",
                                     "Date": "2026-02-19", "Shares": 100}]), \
         patch.object(qq, "fetch_congress_trades", new_callable=AsyncMock,
                      return_value=[{"Ticker": "QQTEST_GOOG",
                                     "Representative": "QQTEST_Pelosi",
                                     "TransactionDate": "2026-02-18"}]), \
         patch.object(qq, "fetch_patent_momentum", new_callable=AsyncMock,
                      return_value=[{"Date": "2026-02-15", "Momentum": 0.85}]):
        r = await qq.sync_all(db, patent_tickers=["QQTEST_AAPL"])
    assert r["configured"] is True
    assert r["insider_upserted"] == 1
    assert r["congress_upserted"] == 1
    assert r["patents_upserted"] == 1
    assert r["patent_tickers"] == ["QQTEST_AAPL"]
    await _reset()
