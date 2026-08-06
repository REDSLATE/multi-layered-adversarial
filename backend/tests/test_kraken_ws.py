"""Kraken WS real-time layer tests (2026-08-05)."""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, "/app/backend")

from shared.market_data import kraken_ws as kw  # noqa: E402

pytestmark = pytest.mark.tripwire


@pytest.fixture(autouse=True)
def _clean():
    kw.reset_for_tests()
    yield
    kw.reset_for_tests()


def test_tick_updates_quote_and_partial_bar():
    import time
    m0 = time.monotonic()
    now = datetime(2026, 8, 5, 12, 0, 30, tzinfo=timezone.utc)
    kw.on_tick("BTC/USD", 100.0, 100.2, 100.1, 5_000_000.0, 50,
               now_mono=m0, now_dt=now)
    q = kw.get_live_quote("BTC/USD", max_age_s=999)
    assert q["bid"] == 100.0 and q["ask"] == 100.2
    assert q["volume_24h_usd"] == 5_000_000.0
    pb = kw.current_partial_bar("BTC/USD")
    assert pb["partial"] and pb["o"] == pb["c"] == 100.1
    # same minute: h/l/c update, open stays
    kw.on_tick("BTC/USD", 0, 0, 101.0, None, 50,
               now_mono=m0, now_dt=now.replace(second=45))
    pb = kw.current_partial_bar("BTC/USD")
    assert pb["o"] == 100.1 and pb["h"] == 101.0 and pb["c"] == 101.0
    # new minute → new bar
    kw.on_tick("BTC/USD", 0, 0, 101.5, None, 50,
               now_mono=m0, now_dt=now.replace(minute=1))
    assert kw.current_partial_bar("BTC/USD")["o"] == 101.5


def test_stale_quote_expires():
    kw.on_tick("ETH/USD", 10.0, 10.1, 10.05, 1.0, 50, now_mono=0.0)
    assert kw.get_live_quote("ETH/USD", max_age_s=0.0) is None


def test_thrust_trigger_fires_and_resets_baseline():
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    # baseline at 100
    assert not kw.on_tick("ICNT/USD", 0, 0, 100.0, None, 50,
                          now_mono=0.0, now_dt=now)
    # +0.3% — below 50bps threshold
    assert not kw.on_tick("ICNT/USD", 0, 0, 100.3, None, 50,
                          now_mono=5.0, now_dt=now)
    # +0.6% from baseline → HOT
    assert kw.on_tick("ICNT/USD", 0, 0, 100.6, None, 50,
                      now_mono=10.0, now_dt=now)
    assert kw.take_hot_symbols() == {"ICNT/USD"}
    assert kw.take_hot_symbols() == set()  # drained
    # baseline reset to 100.6 → +0.3% more does NOT refire
    assert not kw.on_tick("ICNT/USD", 0, 0, 100.9, None, 50,
                          now_mono=15.0, now_dt=now)


def test_baseline_expires_after_60s():
    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    kw.on_tick("SOL/USD", 0, 0, 100.0, None, 50, now_mono=0.0, now_dt=now)
    # 2 minutes later a +1% tick only REBASES (old baseline expired)
    assert not kw.on_tick("SOL/USD", 0, 0, 101.0, None, 50,
                          now_mono=120.0, now_dt=now)


def test_wiring():
    scanner = open("/app/backend/momentum/momentum_scanner.py").read()
    assert "only_symbols" in scanner and "_with_partial_bar" in scanner
    assert "get_live_quote" in scanner
    enrich = open(
        "/app/backend/shared/market_data/crypto_snapshot_enrichment.py").read()
    assert "WS_LIVE" in enrich
    life = open("/app/backend/server_modules/lifespan.py").read()
    assert "kraken_ws_task" in life
