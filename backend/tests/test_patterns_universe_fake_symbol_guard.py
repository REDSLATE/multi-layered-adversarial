"""Fake-symbol guard on patterns_universe — regression fence (2026-02-28).

Doctrine: adding demo/test/mock/fake/sample/example symbols to
`patterns_universe` is BLOCKED at the write path AND filtered from
the read paths. Prevents the DEMOB6B6-class pollution (Finnhub
sandbox returned synthetic bars for that symbol → 35 fake barracuda
BUY intents in the audit trail).

If a future refactor removes the guard or reorders the check, these
tests catch it before another sandbox symbol pollutes the audit.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/app/backend")


def test_guard_matches_demo_prefix():
    from routes.data_stack_admin import _is_fake_symbol
    assert _is_fake_symbol("DEMOB6B6") is True
    assert _is_fake_symbol("DEMO_AAPL") is True
    assert _is_fake_symbol("demo_lower") is True   # case-insensitive


def test_guard_matches_test_mock_fake_prefixes():
    from routes.data_stack_admin import _is_fake_symbol
    for sym in ["TEST1", "MOCK_NVDA", "FAKE_SPY", "SAMPLE_A", "EXAMPLE_B", "SYN_X"]:
        assert _is_fake_symbol(sym) is True, f"guard missed {sym!r}"


def test_guard_does_not_match_real_symbols():
    """Critical negative — the guard MUST NOT accidentally flag real
    tickers. NVDA, SPY, TSLA, ETH/USD, and other legitimate names
    have to pass. A false positive would silently drop live data."""
    from routes.data_stack_admin import _is_fake_symbol
    for sym in [
        "NVDA", "SPY", "TSLA", "AAPL", "MSFT", "AMD", "AMC",  # 'AM' is close
        "META", "GOOGL", "AMZN", "TSMC", "SYMC",              # 'SYM' near 'SYN'
        "DEM",                                                  # too short, no complete word
        "BTC/USD", "ETH/USD", "SOL/USD", "ADA/USD",
    ]:
        assert _is_fake_symbol(sym) is False, (
            f"guard false-positive on real symbol {sym!r} — check the "
            f"regex prefix list"
        )


def test_guard_handles_non_str_input():
    from routes.data_stack_admin import _is_fake_symbol
    assert _is_fake_symbol(None) is False
    assert _is_fake_symbol("") is False
    assert _is_fake_symbol(12345) is False
    assert _is_fake_symbol([]) is False


def test_guard_strips_whitespace_before_matching():
    """A user pasting `  DEMOB6B6  ` from a spreadsheet must still
    get rejected. Otherwise the guard is trivially bypassable."""
    from routes.data_stack_admin import _is_fake_symbol
    assert _is_fake_symbol("  DEMOB6B6") is True
    assert _is_fake_symbol("\tTEST1") is True


def test_filter_drops_polluted_rows_only():
    from routes.data_stack_admin import _filter_fake_symbols
    rows = [
        {"symbol": "NVDA", "lane": "equity", "active": True},
        {"symbol": "DEMOB6B6", "lane": "equity", "active": True},
        {"symbol": "SPY", "lane": "equity", "active": True},
        {"symbol": "TEST_A", "lane": "equity", "active": True},
    ]
    out = _filter_fake_symbols(rows)
    kept = {r["symbol"] for r in out}
    assert kept == {"NVDA", "SPY"}, f"filter dropped wrong set: {kept}"


def test_filter_handles_missing_symbol_field():
    from routes.data_stack_admin import _filter_fake_symbols
    rows = [
        {"lane": "equity"},              # no symbol key
        {"symbol": None},                # None symbol
        {"symbol": "NVDA"},
    ]
    out = _filter_fake_symbols(rows)
    assert len(out) == 3, "filter should tolerate malformed rows"
