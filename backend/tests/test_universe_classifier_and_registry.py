"""Universe classifier + doctrine registry tripwires.

Doctrine pin (2026-02-19, operator-vetted):
    The classifier must NEVER silently default into a scored
    doctrine. An equity with no pinned roster hit, no market_cap_band,
    and no strategy hint MUST resolve to `UNKNOWN`, and the registry
    MUST return a NO_DATA-shaped packet (not REJECT). This is the
    "fail loud vs fail confident" invariant that keeps classification
    gaps visible in the funnel.
"""
from __future__ import annotations

import pytest

from shared.doctrine.registry import dispatch, is_registered
from shared.doctrine.universe_classifier import (
    ETF_SYMBOLS,
    LARGE_CAP_SYMBOLS,
    UniverseClass,
    classify_universe,
)


# ─── universe classifier ─────────────────────────────────────────────


@pytest.mark.tripwire
def test_classify_crypto_lane():
    assert classify_universe({"lane": "crypto", "symbol": "BTCUSD"}) is UniverseClass.CRYPTO


@pytest.mark.tripwire
def test_classify_explicit_small_cap_band():
    for band in ("small", "micro", "nano"):
        got = classify_universe({"lane": "equity", "symbol": "ABC", "market_cap_band": band})
        assert got is UniverseClass.SMALL_CAP_MOMENTUM, band


@pytest.mark.tripwire
def test_classify_small_cap_strategy_hint():
    for strat in ("gap_and_go", "micro_pullback"):
        got = classify_universe({"lane": "equity", "symbol": "ABC", "strategy": strat})
        assert got is UniverseClass.SMALL_CAP_MOMENTUM, strat


@pytest.mark.tripwire
def test_classify_explicit_large_cap_band():
    for band in ("large", "mega"):
        got = classify_universe({"lane": "equity", "symbol": "ABC", "market_cap_band": band})
        assert got is UniverseClass.LARGE_CAP, band


@pytest.mark.tripwire
def test_classify_etf_roster_beats_lane_default():
    # SPY is not in LARGE_CAP_SYMBOLS but IS in ETF_SYMBOLS.
    assert "SPY" in ETF_SYMBOLS
    assert "SPY" not in LARGE_CAP_SYMBOLS
    got = classify_universe({"lane": "equity", "symbol": "SPY"})
    assert got is UniverseClass.ETF


@pytest.mark.tripwire
def test_classify_pinned_mega_cap_roster():
    got = classify_universe({"lane": "equity", "symbol": "NVDA"})
    assert got is UniverseClass.LARGE_CAP


@pytest.mark.tripwire
def test_classify_equity_with_no_hints_is_unknown():
    """The invariant: no roster hit, no band, no strategy → UNKNOWN.

    This must NEVER silently become LARGE_CAP. If the operator
    wants a symbol treated as large-cap, that decision belongs in
    the pinned roster or the enricher, not as an implicit fallback
    inside the classifier.
    """
    got = classify_universe({"lane": "equity", "symbol": "ZZZZ_NOT_ON_ANY_ROSTER"})
    assert got is UniverseClass.UNKNOWN


@pytest.mark.tripwire
def test_classify_missing_lane_is_unknown():
    got = classify_universe({"symbol": "AAPL"})
    # AAPL is on the pinned roster — pinned-roster hit still fires
    # even without a lane (that's the point of the pin).
    assert got is UniverseClass.LARGE_CAP
    # But an unpinned symbol with no lane truly is UNKNOWN.
    got2 = classify_universe({"symbol": "ZZZZ"})
    assert got2 is UniverseClass.UNKNOWN


@pytest.mark.tripwire
def test_classify_non_dict_input():
    assert classify_universe(None) is UniverseClass.UNKNOWN  # type: ignore[arg-type]
    assert classify_universe("banana") is UniverseClass.UNKNOWN  # type: ignore[arg-type]


@pytest.mark.tripwire
def test_classify_explicit_beats_pinned_roster():
    """Explicit small-cap band on an AAPL snapshot still routes to
    small-cap. Explicit hints beat pinned rosters — the operator
    might be re-classifying a stock for a specific strategy."""
    got = classify_universe({"lane": "equity", "symbol": "AAPL", "market_cap_band": "small"})
    assert got is UniverseClass.SMALL_CAP_MOMENTUM


# ─── doctrine registry ──────────────────────────────────────────────


@pytest.mark.tripwire
def test_registry_has_all_four_default_builders_wired():
    """Registry must wire every non-UNKNOWN universe class on import.
    A missing builder is a silent hole — an intent routes to UNKNOWN
    and gets NO_DATA'd instead of scored."""
    for uc in (UniverseClass.LARGE_CAP, UniverseClass.SMALL_CAP_MOMENTUM,
               UniverseClass.ETF, UniverseClass.CRYPTO):
        assert is_registered(uc), f"{uc.name} builder not registered"


@pytest.mark.tripwire
def test_registry_unknown_snapshot_returns_no_data_not_reject():
    """The requested test case (2026-02-19 review):
    UNKNOWN → NO_DATA packet, never a scored REJECT."""
    packet = dispatch({"lane": "equity", "symbol": "ZZZZ_NOT_ON_ANY_ROSTER"})
    assert packet["universe_class"] == "UNKNOWN"
    assert packet["base_labels"]["quality"] == "NO_DATA"
    assert packet["doctrine_version"] == "unknown_universe_no_data_v1"
    # Empty seats — no scoring happened.
    assert packet["seats"] == {}


@pytest.mark.tripwire
def test_registry_dispatches_large_cap_for_pinned_symbol():
    packet = dispatch({
        "lane": "equity",
        "symbol": "NVDA",
        "price": 850.0,
        "gap_pct": 1.5,
        "relative_volume": 2.0,
        "market_regime": "strong",
        "spread_bps": 8,
    })
    assert packet["universe_class"] == "LARGE_CAP"
    assert packet["doctrine_version"] == "large_cap_equity_v1"


@pytest.mark.tripwire
def test_registry_dispatches_etf_via_large_cap_builder():
    """ETFs route through the large-cap doctrine builder (shared
    liquidity/regime scoring) but the packet must be stamped as ETF
    universe so Patent J can graduate ETF slices independently."""
    packet = dispatch({
        "lane": "equity",
        "symbol": "SPY",
        "price": 500.0,
        "gap_pct": 0.5,
        "relative_volume": 1.5,
        "market_regime": "strong",
        "spread_bps": 4,
    })
    assert packet["universe_class"] == "ETF"
    # Uses the large-cap builder underneath.
    assert packet["doctrine_version"] == "large_cap_equity_v1"


@pytest.mark.tripwire
def test_registry_crypto_snapshot_routes_to_crypto_doctrine():
    packet = dispatch({"lane": "crypto", "symbol": "BTCUSD"})
    assert packet["universe_class"] == "CRYPTO"
    # Doctrine version is owned by the crypto sidecar — assert it
    # differs from the large-cap version so we know crypto wasn't
    # accidentally routed to the equity builder.
    assert packet["doctrine_version"] != "large_cap_equity_v1"
