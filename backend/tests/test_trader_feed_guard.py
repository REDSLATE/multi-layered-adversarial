"""Tests for /app/trader/feed_guard.py — L1 sanity guard.

Covers each rejection path individually so a regression in one
check (stale, absolute spread, anomaly, price jump, dual-source)
surfaces cleanly.
"""
from __future__ import annotations

import asyncio
import sys
import time

import pytest

sys.path.insert(0, "/app")

from trader import feed_guard, spread, store  # noqa: E402


@pytest.fixture()
def fresh_store(tmp_path, monkeypatch):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    store.init(
        str(tmp_path / "executions.sqlite"),
        str(tmp_path / "jsonl"),
    )
    spread._latest.clear()
    yield tmp_path
    store.close()
    loop.close()
    asyncio.set_event_loop(None)


def _make_row(**overrides):
    base = {
        "ts": "2026-07-02T00:00:00+00:00",
        "pair": "TSLA", "lane": "equity",
        "bid": 400.0, "ask": 400.05, "last": 400.02,
        "spread_abs": 0.05, "spread_bps": 1.25,
        "source": "webull_mqtt",
        "l1_age_ms": 100,
    }
    base.update(overrides)
    return base


def test_ok_row_passes(fresh_store):
    ok, reason, details = feed_guard.validate_l1("TSLA", _make_row())
    assert ok is True
    assert reason == "ok"
    assert details["symbol"] == "TSLA"


def test_missing_quote_falls_open(fresh_store):
    """No cached reading is neutral, not a rejection — brains can
    still work off OHLC if that's all we have."""
    ok, reason, _ = feed_guard.validate_l1("TSLA", None)
    assert ok is True
    assert reason == "no_quote_cache"


def test_rejects_stale_quote(fresh_store, monkeypatch):
    monkeypatch.setenv("TRADER_GUARD_MAX_AGE_MS", "5000")
    ok, reason, details = feed_guard.validate_l1(
        "TSLA", _make_row(l1_age_ms=9999),
    )
    assert ok is False
    assert "stale_quote" in reason
    assert details["age_ms"] == 9999


def test_rejects_absurd_spread(fresh_store, monkeypatch):
    monkeypatch.setenv("TRADER_GUARD_MAX_SPREAD_BPS", "100")
    ok, reason, details = feed_guard.validate_l1(
        "TSLA", _make_row(spread_bps=250.0),
    )
    assert ok is False
    assert "spread_absurd" in reason
    assert details["spread_bps"] == 250.0


def test_rejects_spread_anomaly_vs_history(fresh_store, monkeypatch):
    """A 60 bps spike when the median is 3 bps is the exact old
    feed-corruption pattern this check is guarding against."""
    monkeypatch.setenv("TRADER_GUARD_SPREAD_ANOM_MULT", "5.0")
    # Seed history
    for i in range(15):
        store.record_spread_tick({
            "ts": f"2026-07-02T00:00:{i:02d}+00:00",
            "pair": "TSLA", "lane": "equity",
            "bid": 400.0, "ask": 400.05, "last": 400.02,
            "spread_abs": 0.05, "spread_bps": 3.0,
            "source": "webull_mqtt",
        })
    ok, reason, details = feed_guard.validate_l1(
        "TSLA", _make_row(spread_bps=60.0),
    )
    assert ok is False
    assert "spread_anomaly" in reason
    assert details["spread_median_bps"] == pytest.approx(3.0)


def test_rejects_price_jump(fresh_store, monkeypatch):
    monkeypatch.setenv("TRADER_GUARD_MAX_PX_JUMP_PCT", "0.05")
    monkeypatch.setenv("TRADER_GUARD_SPREAD_ANOM_MULT", "10.0")
    for i in range(10):
        store.record_spread_tick({
            "ts": f"2026-07-02T00:00:{i:02d}+00:00",
            "pair": "TSLA", "lane": "equity",
            "bid": 400.0, "ask": 400.05, "last": 400.02,
            "spread_abs": 0.05, "spread_bps": 1.25,
            "source": "webull_mqtt",
        })
    ok, reason, details = feed_guard.validate_l1(
        "TSLA", _make_row(last=450.0, bid=449.5, ask=450.5,
                          spread_bps=1.25),  # keep spread ok
    )
    assert ok is False
    assert "price_jump" in reason
    assert details["px_jump_pct"] > 5.0


def test_rejects_dual_source_divergence(fresh_store, monkeypatch):
    """The old corruption showed up as MQTT and HTTP snapshot
    disagreeing wildly — this check catches that immediately."""
    monkeypatch.setenv("TRADER_GUARD_DUAL_SRC_MAX_BPS", "10.0")
    monkeypatch.setenv("TRADER_GUARD_SPREAD_ANOM_MULT", "100.0")
    monkeypatch.setenv("TRADER_GUARD_MAX_SPREAD_BPS", "10000")
    # Seed a fresh HTTP-snapshot reading (different source)
    from datetime import datetime, timezone
    now_ts = datetime.now(timezone.utc).isoformat()
    store.record_spread_tick({
        "ts": now_ts,
        "pair": "TSLA", "lane": "equity",
        "bid": 400.0, "ask": 400.05, "last": 400.02,
        "spread_abs": 0.05, "spread_bps": 1.25,
        "source": "webull",  # HTTP poller source
    })
    # Current reading is MQTT showing an inflated spread
    ok, reason, details = feed_guard.validate_l1(
        "TSLA", _make_row(spread_bps=45.0, source="webull_mqtt"),
    )
    assert ok is False, f"expected rejection, got ok. details={details}"
    assert "dual_src_divergence" in reason
    assert details["dual_src_other"] == "webull"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
