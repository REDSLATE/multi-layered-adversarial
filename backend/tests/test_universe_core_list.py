"""Core liquid list + quality-score ranking + universe cap knobs
(2026-07-24 — operator: garbage-ticker restriction, target 150)."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.universe.refresher import (
    DEFAULT_CORE_EQUITY,
    _apply_hysteresis,
    _apply_quality_filters,
    _core_equity_candidates,
    _dedupe_and_merge,
    _quality_score,
)


def test_default_core_list_has_quality_names():
    for sym in ("SPY", "AAPL", "MSFT", "NVDA", "TSLA", "META"):
        assert sym in DEFAULT_CORE_EQUITY
    assert 40 <= len(DEFAULT_CORE_EQUITY) <= 100


def test_core_candidates_respect_operator_override():
    rows = _core_equity_candidates({"core_equity_symbols": ["aapl", " msft "]})
    assert [r["canonical_symbol"] for r in rows] == ["AAPL", "MSFT"]
    assert all(r["_core"] for r in rows)
    # Empty/absent → defaults
    rows = _core_equity_candidates({})
    assert len(rows) == len(DEFAULT_CORE_EQUITY)


def test_core_rows_bypass_hysteresis_and_price_floor():
    merged = _dedupe_and_merge(_core_equity_candidates({}))
    admitted = _apply_hysteresis(merged, set(), admit_cap=0)  # pins/core only
    assert len(admitted) == len(DEFAULT_CORE_EQUITY)
    assert all(r["_admit_reason"] == "core_liquid" for r in admitted)
    kept, dropped = _apply_quality_filters(admitted, "equity", min_price_override=5.0)
    assert not dropped, "core rows (price unknown=0) must not be price-filtered"


def test_quality_score_prefers_liquid_movers_over_thin_pumps():
    thin_pump = {"change_pct": 250.0, "volume": 80_000, "price": 0.42}
    liquid_mover = {"change_pct": 6.0, "volume": 40_000_000, "price": 180.0}
    assert _quality_score(liquid_mover) > _quality_score(thin_pump), (
        "a +6% move on 40M shares at $180 must outrank a +250% penny pump"
    )


def test_quality_score_price_band():
    base = {"change_pct": 10.0, "volume": 1_000_000}
    mid = _quality_score({**base, "price": 50.0})
    penny = _quality_score({**base, "price": 0.5})
    assert mid > penny


def test_dedupe_merges_core_flag_with_screener_row():
    rows = _dedupe_and_merge([
        {"canonical_symbol": "AAPL", "source_reason": "core_liquid",
         "_core": True, "change_pct": 0.0, "volume": 0.0, "price": 0.0},
        {"canonical_symbol": "AAPL", "source_reason": "top_gainer",
         "change_pct": 4.2, "volume": 55_000_000, "price": 210.0},
    ])
    assert len(rows) == 1
    r = rows[0]
    assert r["core"] is True
    assert r["change_pct"] == 4.2  # screener signal preserved
    assert set(r["source_reasons"]) == {"core_liquid", "top_gainer"}
