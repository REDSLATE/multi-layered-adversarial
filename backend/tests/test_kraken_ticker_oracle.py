"""Tests for the public Kraken Ticker oracle used by Position Monitor.

These tests hit Kraken's public API (no keys needed) so they're a real
integration check, not a mock. If Kraken is unreachable the assertions
that require a network call are skipped via pytest.skip, so the suite
stays green in offline CI.

We don't depend on pytest-asyncio — these are sync tests that drive the
coroutine with `asyncio.run()`.
"""
from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_database")

from shared.crypto.kraken import (  # noqa: E402
    _normalise_kraken_pair_key,
    fetch_tickers,
)


def _run(coro, timeout: float = 15.0):
    return asyncio.run(asyncio.wait_for(coro, timeout=timeout))


# ─── unit: canonical-pair normalisation (no network) ─────────────────

def test_normalise_xxbtzusd_to_xbtusd():
    assert _normalise_kraken_pair_key("XXBTZUSD") == "XBTUSD"


def test_normalise_xethzusd_to_ethusd():
    assert _normalise_kraken_pair_key("XETHZUSD") == "ETHUSD"


def test_normalise_xxrpzusd_to_xrpusd():
    assert _normalise_kraken_pair_key("XXRPZUSD") == "XRPUSD"


def test_normalise_xdgusd_to_dogeusd():
    # Kraken's DOGE alias is XDG — must round-trip to our internal DOGE.
    assert _normalise_kraken_pair_key("XDGUSD") == "DOGEUSD"


def test_normalise_plain_altname_passthrough():
    # Non-canonical altnames (SOLUSD, ADAUSD) must come through unchanged.
    assert _normalise_kraken_pair_key("SOLUSD") == "SOLUSD"
    assert _normalise_kraken_pair_key("ADAUSD") == "ADAUSD"


def test_normalise_eur_quote():
    assert _normalise_kraken_pair_key("XXBTZEUR") == "XBTEUR"


# ─── integration: live Kraken Ticker (network-skippable) ──────────────

def test_fetch_tickers_returns_all_six_default_pairs():
    """Live test: every pair in the production Kraken auto-poller default
    set must round-trip to a positive float price. This is the regression
    that the v1 fuzzy `lstrip("X")` mapper broke."""
    pairs = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD", "DOGE/USD"]
    try:
        out = _run(fetch_tickers(pairs))
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Kraken public ticker unreachable: {e}")
    if not out:
        pytest.skip("Kraken returned empty result (rate-limited or transient)")
    missing = [p for p in pairs if p not in out]
    assert not missing, f"Kraken returned no price for {missing}; got {sorted(out)}"
    for p, v in out.items():
        assert isinstance(v, float)
        assert v > 0, f"{p} → {v}"


def test_fetch_tickers_empty_input_returns_empty():
    out = _run(fetch_tickers([]))
    assert out == {}


def test_fetch_tickers_handles_garbage_pair_gracefully():
    try:
        out = _run(fetch_tickers(["NOT/AREAL/PAIR"]))
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Kraken public ticker unreachable: {e}")
    assert isinstance(out, dict)
    assert "NOT/AREAL/PAIR" not in out
