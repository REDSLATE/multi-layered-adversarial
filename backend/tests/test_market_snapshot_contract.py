"""Contract test for the brain-facing market-data snapshot (2026-05-31).

Pins the field set that `build_market_snapshot()` returns so the
published API doc (`BRAIN_API_QUICKSTART.md` § 1) and the wire stay
in sync. Chevelle agent caught a drift between the two earlier today
and we don't want it to recur silently.

Pinned fields:
  - Labeler features: relative_volume, has_news (existing)
  - Price block: price, ohlc, price_ok, price_reason,
    last_bar_age_sec, asof (added 2026-05-31)
  - NOT pinned: spread_bps (intentionally absent — brains pull live
    quotes broker-direct)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

import pytest

from db import db
from namespaces import SHARED_OHLCV_BARS
from shared.market_data.feature_service import build_market_snapshot


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
async def seeded_bars():
    """Seed 10 5m bars for a unique test symbol so the snapshot has
    enough basis to compute relative_volume AND return a real price."""
    sym = f"TST{uuid.uuid4().hex[:6].upper()}"
    src = "finnhub_equity"
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(10):
        ts = (now - timedelta(minutes=5 * (10 - i))).isoformat()
        rows.append({
            "source": src, "symbol": sym, "tf": "5m",
            "ts": ts,
            "o": 100.0 + i, "h": 102.0 + i, "l": 99.5 + i, "c": 101.0 + i,
            "v": 10_000.0 + i * 100,
        })
    await db[SHARED_OHLCV_BARS].insert_many(rows)
    yield {"symbol": sym, "source": src}
    await db[SHARED_OHLCV_BARS].delete_many({"symbol": sym, "source": src})


@pytest.mark.asyncio
async def test_snapshot_includes_price_field(seeded_bars):
    """The snapshot MUST include `price` (last-bar close). Chevelle
    agent's audit found `price` missing — that gap is closed here.
    Without `price`, downstream brains fall through to broker quotes
    or sentinels like spread_bps=9999."""
    snap = await build_market_snapshot(
        seeded_bars["symbol"], source=seeded_bars["source"], include_news=False,
    )
    assert "price" in snap, "snapshot must return a `price` field"
    assert snap["price"] is not None, "price must be populated when bars exist"
    assert snap["price_ok"] is True
    # Last-bar close = 101.0 + 9 = 110.0 (10th bar, index 9 in our seed).
    assert snap["price"] == 110.0


@pytest.mark.asyncio
async def test_snapshot_includes_ohlc_block(seeded_bars):
    """Full OHLC for the last bar — brains may want o/h/l beyond just
    close, e.g. for premarket-high checks."""
    snap = await build_market_snapshot(
        seeded_bars["symbol"], source=seeded_bars["source"], include_news=False,
    )
    assert snap["ohlc"] is not None
    assert set(snap["ohlc"].keys()) == {"o", "h", "l", "c", "v"}
    # Verify the values match the seeded last bar.
    assert snap["ohlc"]["c"] == 110.0
    assert snap["ohlc"]["o"] == 109.0


@pytest.mark.asyncio
async def test_snapshot_includes_asof_and_last_bar_age(seeded_bars):
    """The `asof` field is a doc-promised alias for the price's
    timestamp. `last_bar_age_sec` is the freshness signal."""
    snap = await build_market_snapshot(
        seeded_bars["symbol"], source=seeded_bars["source"], include_news=False,
    )
    assert snap["asof"] is not None
    assert isinstance(snap["asof"], str)
    assert snap["last_bar_age_sec"] is not None
    assert snap["last_bar_age_sec"] >= 0


@pytest.mark.asyncio
async def test_snapshot_does_NOT_include_spread_bps(seeded_bars):
    """Doctrine pin: `spread_bps` is INTENTIONALLY NOT in MC's
    snapshot — brains pull live quotes broker-direct because MC
    doesn't hit the broker on the snapshot path (would break
    `derived_evidence_only`). If a future agent adds `spread_bps`
    here, they need to read the doc + revisit the doctrine first."""
    snap = await build_market_snapshot(
        seeded_bars["symbol"], source=seeded_bars["source"], include_news=False,
    )
    assert "spread_bps" not in snap, (
        "spread_bps must NOT appear on MC's snapshot. Brains pull live "
        "quotes broker-direct. See _fetch_latest_bar doctrine note."
    )


@pytest.mark.asyncio
async def test_snapshot_for_unknown_symbol_returns_null_price():
    """No bars → price=None with a typed reason. Must NOT raise; must
    NOT silently return 0.0 or a stale sentinel. Brains downstream
    detect None and fall back to their own quote source cleanly."""
    snap = await build_market_snapshot(
        f"NOEXIST{uuid.uuid4().hex[:6].upper()}",
        source="finnhub_equity",
        include_news=False,
    )
    assert snap["price"] is None
    assert snap["price_ok"] is False
    assert snap["price_reason"] == "no_bars_for_symbol"
    assert snap["ohlc"] is None
    assert snap["asof"] is None


@pytest.mark.asyncio
async def test_snapshot_keys_pin_for_doc_alignment(seeded_bars):
    """Field-set lock so the published quickstart doc and the wire
    stay synchronized. Adding NEW fields is fine (additive); RENAMING
    or REMOVING an expected field requires a doc update + this test
    update in the same PR."""
    snap = await build_market_snapshot(
        seeded_bars["symbol"], source=seeded_bars["source"], include_news=True,
    )
    expected = {
        # Identity
        "symbol", "tf", "source", "computed_at",
        # Labeler features (existing)
        "relative_volume", "relative_volume_ok", "relative_volume_reason",
        "last_bar_ts", "current_v", "avg_v", "basis_bars",
        # News (existing)
        "has_news", "has_news_ok", "has_news_reason",
        "has_news_source", "has_news_from_cache",
        # Price block (NEW 2026-05-31 — Chevelle audit fix)
        "price", "ohlc", "price_ok", "price_reason",
        "last_bar_age_sec", "asof",
    }
    actual = set(snap.keys())
    missing = expected - actual
    assert not missing, f"snapshot is missing doc-promised fields: {missing}"
