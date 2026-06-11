"""Equity doctrine enricher — unit tests with mocked Webull client."""
from __future__ import annotations

import asyncio
import pytest

from shared.snapshot_enrich import equity_doctrine as ed


class _FakeClient:
    def __init__(self, *, snap=None, instr=None, bars=None, screener=None):
        self._snap = snap
        self._instr = instr
        self._bars = bars or []
        self._screener = screener or {}

    def equity_snapshot(self, sym):
        return self._snap

    def instrument(self, sym):
        return self._instr

    def equity_bars(self, sym, timespan="M1", count=30):
        return self._bars

    def most_active_map(self):
        return self._screener


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _bar(o, h, l, c, v=1000):
    return {"open": o, "high": h, "low": l, "close": c, "volume": v}


@pytest.fixture
def patch_client(monkeypatch):
    holder = {}

    def install(client):
        holder["c"] = client
        monkeypatch.setattr(
            "shared.market_data.webull_quotes.get_quotes_client",
            lambda: client,
        )

    return install


def test_returns_base_when_client_missing(monkeypatch):
    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: None,
    )
    base = {"symbol": "AAPL", "lane": "equity", "price": 100.0}
    out = _run(ed.enrich_equity_doctrine_snapshot("AAPL", base))
    assert out == base


def test_populates_gap_pct_and_spread(patch_client):
    patch_client(_FakeClient(
        snap={"price": 105.0, "pre_close": 100.0, "bid": 104.95, "ask": 105.05,
              "change_ratio": 0.05, "volume": 1_000_000},
        instr={"fractionable": True, "shortable": True, "exchange_code": "NSQ"},
        bars=[],
    ))
    out = _run(ed.enrich_equity_doctrine_snapshot("AAPL", {"symbol": "AAPL", "lane": "equity"}))
    assert out["price"] == 105.0
    assert out["gap_pct"] == pytest.approx(5.0, abs=0.01)
    assert out["spread_bps"] is not None
    assert 8 <= out["spread_bps"] <= 12  # ~10 bps for $105 spread of $0.10
    assert out["market_cap_band"] == "mega"  # AAPL is mega
    assert out["fractionable"] is True
    assert out["webull_enriched"] is True


def test_market_cap_band_small_for_unlisted(patch_client):
    patch_client(_FakeClient(
        snap={"price": 5.0, "pre_close": 4.5, "bid": 4.99, "ask": 5.01},
        instr={"etf_leveraged_flag": "NO"},  # Webull stamps this on all equities
    ))
    out = _run(ed.enrich_equity_doctrine_snapshot("XYZQ", {"symbol": "XYZQ", "lane": "equity"}))
    # XYZQ is NOT in the mega-cap roster → small (etf_leveraged_flag is
    # NOT a "large" signal because Webull sets it on every US equity).
    assert out["market_cap_band"] == "small"


def test_near_half_or_whole_dollar():
    assert ed._near_half_or_whole_dollar(5.00) is True
    assert ed._near_half_or_whole_dollar(5.49) is True
    assert ed._near_half_or_whole_dollar(5.51) is True
    assert ed._near_half_or_whole_dollar(5.99) is True
    assert ed._near_half_or_whole_dollar(5.25) is False
    assert ed._near_half_or_whole_dollar(0.0) is False


def test_momentum_active_true_when_rising(patch_client):
    bars = [_bar(10, 10.1, 9.9, 10.0)] * 3 + [
        _bar(10.0, 10.2, 9.95, 10.1),
        _bar(10.1, 10.3, 10.0, 10.2),
        _bar(10.2, 10.4, 10.1, 10.3),
        _bar(10.3, 10.5, 10.2, 10.4),
        _bar(10.4, 10.6, 10.3, 10.5),
    ]
    assert ed._momentum_active(bars) is True


def test_momentum_active_false_when_flat(patch_client):
    bars = [_bar(10, 10.1, 9.9, 10.0)] * 6
    assert ed._momentum_active(bars) is False


def test_pullback_low_identifies_lowest_after_peak():
    bars = [
        _bar(10.0, 10.1, 9.9, 10.0),
        _bar(10.0, 10.5, 10.0, 10.5),  # peak
        _bar(10.5, 10.5, 10.2, 10.3),
        _bar(10.3, 10.4, 10.0, 10.1),  # pullback low here
        _bar(10.1, 10.3, 10.05, 10.2),
        _bar(10.2, 10.6, 10.1, 10.55),
    ] * 2  # need ≥10 bars
    pl = ed._pullback_low(bars)
    assert pl is not None
    assert pl <= 10.0


def test_relative_volume_pulled_from_screener(patch_client):
    patch_client(_FakeClient(
        snap={"price": 50.0, "pre_close": 45.0, "bid": 49.95, "ask": 50.05},
        screener={"NVDA": {"relative_volume_10d": "3.45"}},
    ))
    out = _run(ed.enrich_equity_doctrine_snapshot("NVDA", {"symbol": "NVDA", "lane": "equity"}))
    assert out["relative_volume"] == 3.45


def test_pattern_detection_flat_top():
    # 10 M1 bars: 2 bars rising sharply, then 8 tight bars at the top
    bars = [
        _bar(10.0, 10.1, 9.95, 10.05),
        _bar(10.05, 10.50, 10.00, 10.50),  # peak
        _bar(10.50, 10.51, 10.49, 10.50),
        _bar(10.50, 10.51, 10.49, 10.50),
        _bar(10.50, 10.51, 10.49, 10.50),
        _bar(10.50, 10.51, 10.49, 10.50),
        _bar(10.50, 10.51, 10.49, 10.50),
        _bar(10.50, 10.51, 10.49, 10.50),
        _bar(10.50, 10.51, 10.49, 10.50),
        _bar(10.50, 10.51, 10.49, 10.50),
    ]
    p = ed._detect_pattern(bars)
    assert p in ("flat_top_breakout", "bull_flag", "micro_pullback")


def test_no_nearby_resistance_within_band():
    bars = [_bar(100, 101.0, 99.0, 100.5)] * 30
    # Current price 100.4 — nearby high 101.0 is 0.6% above (>0.5%), so "no resistance" within 0.5%
    assert ed._no_nearby_resistance(bars, 100.4) is True
    # Current 100.6 — nearby high 101.0 is only 0.4% above → resistance present
    assert ed._no_nearby_resistance(bars, 100.6) is False


def test_enrich_fail_soft_on_exception(monkeypatch):
    class _Boom:
        def equity_snapshot(self, sym): raise RuntimeError("boom")
        def instrument(self, sym): return None
        def equity_bars(self, *a, **k): return []
        def most_active_map(self): return {}

    monkeypatch.setattr(
        "shared.market_data.webull_quotes.get_quotes_client",
        lambda: _Boom(),
    )
    base = {"symbol": "FAIL", "lane": "equity"}
    out = _run(ed.enrich_equity_doctrine_snapshot("FAIL", base))
    # Should still return SOMETHING — falls back to base (raised inside _enrich_sync caught by async wrapper)
    # OR returns the partial-built out dict if get_snapshot was the only failure.
    assert "symbol" in out
