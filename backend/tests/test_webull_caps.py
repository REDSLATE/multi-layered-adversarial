"""Tests for the Webull pre-trade cap module.

Doctrine pin (operator, 2026-06-10): Webull route ships LIVE on day one
with a small-pilot $3-$10 notional band per ticker plus an explicit
`WEBULL_ARMED=true` gate. These tests pin both invariants so a
mid-flight env change can't silently flatten the safety rail.
"""
import sys
import os

sys.path.insert(0, "/app/backend")

import pytest

from shared.broker.webull_caps import (
    DEFAULT_MAX_NOTIONAL_USD,
    DEFAULT_MIN_NOTIONAL_USD,
    WebullCapBlocked,
    evaluate_webull_order,
    is_webull_armed,
    webull_notional_band,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip any inherited Webull env so each test starts clean."""
    for key in (
        "WEBULL_ARMED",
        "WEBULL_MIN_NOTIONAL_USD",
        "WEBULL_MAX_NOTIONAL_USD",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


def test_default_armed_is_false():
    """Unset → off. Fail-closed by default."""
    assert is_webull_armed() is False


@pytest.mark.parametrize("val", ["true", "True", "1", "yes", "on", "  TRUE  "])
def test_armed_true_values(monkeypatch, val):
    monkeypatch.setenv("WEBULL_ARMED", val)
    assert is_webull_armed() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "", "maybe", "  "])
def test_armed_false_values(monkeypatch, val):
    monkeypatch.setenv("WEBULL_ARMED", val)
    assert is_webull_armed() is False


def test_default_band_is_3_to_10():
    lo, hi = webull_notional_band()
    assert lo == DEFAULT_MIN_NOTIONAL_USD == 3.00
    assert hi == DEFAULT_MAX_NOTIONAL_USD == 10.00


def test_band_widens_from_env(monkeypatch):
    monkeypatch.setenv("WEBULL_MIN_NOTIONAL_USD", "5.00")
    monkeypatch.setenv("WEBULL_MAX_NOTIONAL_USD", "8.00")
    lo, hi = webull_notional_band()
    assert lo == 5.00 and hi == 8.00


def test_band_caps_ceiling_at_100(monkeypatch):
    """Sanity rail: even if operator types 9999, the small-pilot
    ceiling holds at $100 to keep blast radius bounded."""
    monkeypatch.setenv("WEBULL_MAX_NOTIONAL_USD", "9999")
    lo, hi = webull_notional_band()
    assert hi == 100.0


def test_band_floors_below_1_cent(monkeypatch):
    monkeypatch.setenv("WEBULL_MIN_NOTIONAL_USD", "-5")
    lo, hi = webull_notional_band()
    assert lo == 0.01


def test_band_inverted_collapses(monkeypatch):
    """If operator types min > max, the ceiling adjusts up to the
    floor — never invert."""
    monkeypatch.setenv("WEBULL_MIN_NOTIONAL_USD", "20")
    monkeypatch.setenv("WEBULL_MAX_NOTIONAL_USD", "5")
    lo, hi = webull_notional_band()
    assert hi >= lo


def test_band_malformed_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("WEBULL_MIN_NOTIONAL_USD", "abc")
    monkeypatch.setenv("WEBULL_MAX_NOTIONAL_USD", "xyz")
    lo, hi = webull_notional_band()
    assert lo == DEFAULT_MIN_NOTIONAL_USD
    assert hi == DEFAULT_MAX_NOTIONAL_USD


# ── evaluate_webull_order ──────────────────────────────────────────


def test_blocks_when_not_armed():
    """Default disarmed state → every order refused regardless of size."""
    d = evaluate_webull_order(notional_usd=5.00, symbol="AAPL")
    assert d.ok is False
    assert d.armed is False
    assert "WEBULL_NOT_ARMED" in d.reason


def test_allows_when_armed_and_in_band(monkeypatch):
    monkeypatch.setenv("WEBULL_ARMED", "true")
    d = evaluate_webull_order(notional_usd=5.00, symbol="AAPL")
    assert d.ok is True
    assert d.armed is True
    assert d.reason == "WEBULL_CAP_OK"


def test_blocks_below_floor(monkeypatch):
    monkeypatch.setenv("WEBULL_ARMED", "true")
    d = evaluate_webull_order(notional_usd=2.50, symbol="AAPL")
    assert d.ok is False
    assert "BELOW_FLOOR" in d.reason
    assert "AAPL" in d.reason


def test_blocks_above_cap(monkeypatch):
    monkeypatch.setenv("WEBULL_ARMED", "true")
    d = evaluate_webull_order(notional_usd=15.00, symbol="TSLA")
    assert d.ok is False
    assert "ABOVE_CAP" in d.reason
    assert "TSLA" in d.reason


def test_blocks_when_notional_missing(monkeypatch):
    monkeypatch.setenv("WEBULL_ARMED", "true")
    d = evaluate_webull_order(notional_usd=None, symbol="AAPL")
    assert d.ok is False
    assert "MISSING" in d.reason


def test_boundary_floor_is_inclusive(monkeypatch):
    monkeypatch.setenv("WEBULL_ARMED", "true")
    d = evaluate_webull_order(notional_usd=3.00, symbol="AAPL")
    assert d.ok is True


def test_boundary_ceiling_is_inclusive(monkeypatch):
    monkeypatch.setenv("WEBULL_ARMED", "true")
    d = evaluate_webull_order(notional_usd=10.00, symbol="AAPL")
    assert d.ok is True


def test_raise_if_blocked_works():
    d = evaluate_webull_order(notional_usd=5.00, symbol="AAPL")
    with pytest.raises(WebullCapBlocked):
        d.raise_if_blocked()


def test_raise_if_blocked_silent_when_ok(monkeypatch):
    monkeypatch.setenv("WEBULL_ARMED", "true")
    d = evaluate_webull_order(notional_usd=5.00, symbol="AAPL")
    d.raise_if_blocked()  # must NOT raise


def test_crypto_canonical_symbol_in_reason(monkeypatch):
    """The cap evaluator surfaces the symbol verbatim in its reason
    string so the operator can grep the gate log by ticker."""
    monkeypatch.setenv("WEBULL_ARMED", "true")
    d = evaluate_webull_order(notional_usd=20.00, symbol="CRYPTO:BTC-USD")
    assert d.ok is False
    assert "CRYPTO:BTC-USD" in d.reason
