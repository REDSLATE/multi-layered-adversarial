"""2026-02-20 — `bar_source.pick_source` doctrine: brokers are
PRIMARY, polygon/finnhub are BACKUPS, shallow backups are skipped.

Operator pin (verbatim):
    "the primary sources should be the broker themselves.
     Polygon and finnhub should be back up."

These tests pin the selection rule so a future refactor that
re-orders the priority list (or removes the shallow-backup skip)
trips the regression.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import shared.research.bar_source as bar_source


def _mock_db_with_sources(per_source_counts: dict[str, int]):
    """Return a context manager that patches `db[SHARED_OHLCV_BARS]`
    to behave like a Mongo collection where `aggregate(...)` returns
    one row per source with the given bar counts."""
    rows = [{"_id": src, "n": n} for src, n in per_source_counts.items()]

    class _Cursor:
        async def to_list(self, _limit):
            return list(rows)

    mock_coll = MagicMock()
    mock_coll.aggregate = MagicMock(return_value=_Cursor())
    mock_db = MagicMock()
    mock_db.__getitem__ = MagicMock(return_value=mock_coll)
    return patch.object(bar_source, "db", mock_db)


# ── Broker wins even when shallow ────────────────────────────────────
@pytest.mark.asyncio
async def test_broker_picked_even_with_few_bars():
    # Webull just started; only 9 bars on file. Polygon has 500.
    # Doctrine: broker wins. Period.
    with _mock_db_with_sources({"webull": 9, "polygon": 500}):
        src = await bar_source.pick_source("AAPL", "1d")
    assert src == "webull"


@pytest.mark.asyncio
async def test_kraken_pro_picked_even_with_few_bars():
    # Same rule on the crypto lane.
    with _mock_db_with_sources({"kraken_pro": 7, "polygon": 800}):
        src = await bar_source.pick_source("BTC/USD", "1h")
    assert src == "kraken_pro"


# ── Polygon ahead of finnhub when both are deep ──────────────────────
@pytest.mark.asyncio
async def test_polygon_chosen_over_finnhub_when_both_deep():
    # Both backups have plenty of bars. Polygon comes first in
    # SOURCE_PRIORITY so it wins on tie-break.
    with _mock_db_with_sources({"polygon": 800, "finnhub_equity": 2500}):
        src = await bar_source.pick_source("AAPL", "1d")
    assert src == "polygon"


# ── Skip-shallow-backup rule ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_shallow_polygon_skipped_for_deep_finnhub():
    # The exact regression that prompted the new doctrine: polygon
    # has 9 bars (trial data), finnhub has 2511. Polygon is shallow,
    # gets skipped; finnhub gets the score window.
    with _mock_db_with_sources({"polygon": 9, "finnhub_equity": 2511}):
        src = await bar_source.pick_source("AAPL", "1d")
    assert src == "finnhub_equity"


# ── Fall-through when EVERYTHING is shallow ──────────────────────────
@pytest.mark.asyncio
async def test_all_shallow_falls_back_to_deepest():
    # Cold-start symbol — every backup is shallow. Return whichever
    # has the most bars rather than starving research entirely.
    with _mock_db_with_sources({"polygon": 20, "finnhub_equity": 30}):
        src = await bar_source.pick_source("NEWCO", "1d")
    assert src == "finnhub_equity"


# ── Empty case ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_no_bars_returns_none():
    with _mock_db_with_sources({}):
        src = await bar_source.pick_source("NEVER", "1d")
    assert src is None


# ── Unknown source name ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_unknown_source_falls_through_when_alone():
    # A source not in SOURCE_PRIORITY with deep history is still
    # returned via the "deepest wins" last-resort branch.
    with _mock_db_with_sources({"some_new_feed": 1_000}):
        src = await bar_source.pick_source("AAPL", "1d")
    assert src == "some_new_feed"
