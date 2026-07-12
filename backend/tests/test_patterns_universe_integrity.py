"""Universe integrity regression tests.

Locks in the operator's 2026-02-19 directive:
    * `patterns_universe` has EXACTLY 20 active symbols per lane.
    * Known-junk tickers must NEVER appear as active rows.
    * Every active row must carry a `lane` field ("equity"/"crypto").

These are integration tests against the live `test_database` — they
read but do not write.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from db import db  # noqa: E402


UNIVERSE = "patterns_universe"

KNOWN_JUNK = {
    "FB",             # delisted (renamed META)
    "MSFY",           # typo of MSFT
    "HEL31138C",      # synthetic test row
    "HEL5E7DFF",      # synthetic test row
    "NDBC0764B",      # NANO-BANANA synthetic
    "NDBC349F6",      # NANO-BANANA synthetic
}

APPROVED_EQUITY_20 = {
    "AAPL", "AMD", "AMZN", "AVGO", "BABA", "GOOG", "META", "MSFT",
    "NFLX", "NVDA", "ORCL", "PLTR", "SHOP", "TSLA", "TSM", "SPCX",
    "GME", "HOTH", "TEVA", "PFE",
}

APPROVED_CRYPTO_20 = {
    "ADA/USD", "AVAX/USD", "BNB/USD", "BTC/USD", "ETH/USD",
    "LINK/USD", "SOL/USD", "XRP/USD", "DOGE/USD", "DOT/USD",
    "LTC/USD", "ATOM/USD", "ALGO/USD", "XLM/USD", "FIL/USD",
    # 2026-07-11 (iter-27) operator directive: MKR retired
    # (project effectively dead), MATIC deprecated post-Polygon
    # rebrand → replaced with QNT and POL.
    "NEAR/USD", "POL/USD", "UNI/USD", "AAVE/USD", "QNT/USD",
}


@pytest.mark.asyncio
async def test_active_equity_universe_is_exactly_twenty():
    rows = await db[UNIVERSE].find(
        {"lane": "equity", "active": True},
        {"_id": 0, "symbol": 1},
    ).to_list(100)
    symbols = {r["symbol"] for r in rows}
    assert len(symbols) == 20, (
        f"Active equity universe drifted: got {len(symbols)} "
        f"symbols, expected 20. "
        f"Extra={symbols - APPROVED_EQUITY_20}, "
        f"Missing={APPROVED_EQUITY_20 - symbols}"
    )
    assert symbols == APPROVED_EQUITY_20, (
        f"Active equity universe symbols drifted from approved set. "
        f"Extra={symbols - APPROVED_EQUITY_20}, "
        f"Missing={APPROVED_EQUITY_20 - symbols}"
    )


@pytest.mark.asyncio
async def test_active_crypto_universe_is_exactly_twenty():
    rows = await db[UNIVERSE].find(
        {"lane": "crypto", "active": True},
        {"_id": 0, "symbol": 1},
    ).to_list(100)
    symbols = {r["symbol"] for r in rows}
    assert len(symbols) == 20, (
        f"Active crypto universe drifted: got {len(symbols)} "
        f"symbols, expected 20. "
        f"Extra={symbols - APPROVED_CRYPTO_20}, "
        f"Missing={APPROVED_CRYPTO_20 - symbols}"
    )
    assert symbols == APPROVED_CRYPTO_20, (
        f"Active crypto universe symbols drifted from approved set. "
        f"Extra={symbols - APPROVED_CRYPTO_20}, "
        f"Missing={APPROVED_CRYPTO_20 - symbols}"
    )


@pytest.mark.asyncio
async def test_known_junk_never_active():
    """No junk / delisted / synthetic ticker may appear as active,
    regardless of lane."""
    n = await db[UNIVERSE].count_documents({
        "symbol": {"$in": list(KNOWN_JUNK)},
        "active": True,
    })
    assert n == 0, (
        f"Junk resurfaced as active in patterns_universe: {KNOWN_JUNK}"
    )


@pytest.mark.asyncio
async def test_every_active_row_has_lane_field():
    """A row that lands in patterns_universe without a `lane` field
    breaks downstream lane-scoped queries. Assert every active row
    carries one."""
    n_missing = await db[UNIVERSE].count_documents({
        "active": True,
        "$or": [
            {"lane": {"$exists": False}},
            {"lane": None},
            {"lane": ""},
        ],
    })
    assert n_missing == 0, (
        f"{n_missing} active rows in patterns_universe are missing a "
        "lane field"
    )
