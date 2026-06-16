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
        "WEBULL_PCT_OF_BUYING_POWER",
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


def test_default_band_is_1_to_10():
    lo, hi, src = webull_notional_band()
    assert lo == DEFAULT_MIN_NOTIONAL_USD == 1.00
    assert hi == DEFAULT_MAX_NOTIONAL_USD == 10.00
    assert src == "env"


def test_band_widens_from_env(monkeypatch):
    monkeypatch.setenv("WEBULL_MIN_NOTIONAL_USD", "5.00")
    monkeypatch.setenv("WEBULL_MAX_NOTIONAL_USD", "8.00")
    lo, hi, _ = webull_notional_band()
    assert lo == 5.00 and hi == 8.00


def test_band_caps_ceiling_at_sanity_rail(monkeypatch):
    """Sanity rail: even if operator types 9999, the hard sanity
    ceiling holds at $500 to keep blast radius bounded."""
    monkeypatch.setenv("WEBULL_MAX_NOTIONAL_USD", "9999")
    lo, hi, src = webull_notional_band()
    assert hi == 500.0
    assert src == "sanity_ceiling"


def test_band_floors_below_1_cent(monkeypatch):
    monkeypatch.setenv("WEBULL_MIN_NOTIONAL_USD", "-5")
    lo, hi, _ = webull_notional_band()
    assert lo == 0.01


def test_band_inverted_collapses(monkeypatch):
    """If operator types min > max, the ceiling adjusts up to the
    floor — never invert."""
    monkeypatch.setenv("WEBULL_MIN_NOTIONAL_USD", "20")
    monkeypatch.setenv("WEBULL_MAX_NOTIONAL_USD", "5")
    lo, hi, _ = webull_notional_band()
    assert hi >= lo


def test_band_malformed_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("WEBULL_MIN_NOTIONAL_USD", "abc")
    monkeypatch.setenv("WEBULL_MAX_NOTIONAL_USD", "xyz")
    lo, hi, _ = webull_notional_band()
    assert lo == DEFAULT_MIN_NOTIONAL_USD
    assert hi == DEFAULT_MAX_NOTIONAL_USD


def test_band_uses_buying_power_when_supplied(monkeypatch):
    """2026-02-20: when BP is supplied, the ceiling is BP × pct.
    Default pct=5%, so BP=$500 → ceiling=$25, NOT the $10 env default."""
    # Raise env cap so it doesn't bind — we want the BP cap to win.
    monkeypatch.setenv("WEBULL_MAX_NOTIONAL_USD", "500")
    lo, hi, src = webull_notional_band(buying_power_usd=500.0)
    assert hi == 25.00
    assert src == "buying_power"


def test_band_env_caps_buying_power(monkeypatch):
    """Env ceiling acts as an upper bound on the dynamic cap.
    BP=$10,000 × 5% = $500 dyn cap, but env says $50 → ceiling=$50."""
    monkeypatch.setenv("WEBULL_MAX_NOTIONAL_USD", "50")
    lo, hi, src = webull_notional_band(buying_power_usd=10_000.0)
    assert hi == 50.00
    assert src == "env"


def test_band_pct_env_override(monkeypatch):
    """Operator can tune `WEBULL_PCT_OF_BUYING_POWER` to size differently."""
    monkeypatch.setenv("WEBULL_PCT_OF_BUYING_POWER", "0.10")
    monkeypatch.setenv("WEBULL_MAX_NOTIONAL_USD", "500")
    lo, hi, src = webull_notional_band(buying_power_usd=500.0)
    assert hi == 50.00  # 10% of $500
    assert src == "buying_power"


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
    d = evaluate_webull_order(notional_usd=0.50, symbol="AAPL")
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
    d = evaluate_webull_order(notional_usd=1.00, symbol="AAPL")
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


# ── doctrine pin: $1 fractional floor (2026-02-19 rev) ─────────────


def test_one_dollar_fractional_intent_passes(monkeypatch):
    """Doctrine pin (operator, 2026-02-19): Webull supports fractional
    shares starting at a $1 notional minimum. The gate floor was lowered
    $3 → $1 to align with that. A $1.00 BUY intent on AAPL MUST pass
    the cap gate — if a future env-tweak or doctrine drift raises the
    floor back above $1 without intent, this test fails loudly."""
    monkeypatch.setenv("WEBULL_ARMED", "true")
    d = evaluate_webull_order(notional_usd=1.00, symbol="AAPL")
    assert d.ok is True, (
        "$1.00 notional must clear the floor — Webull's fractional "
        "minimum is $1, MC must not refuse smaller-than-$3 intents"
    )


def test_intermediate_two_dollar_intent_passes(monkeypatch):
    """The exact case that motivated the floor drop: a fractional
    BUY around $2 (e.g., 0.05 shares of a $40 ticker) used to be
    rejected under the $3 floor; with the $1 floor it MUST pass."""
    monkeypatch.setenv("WEBULL_ARMED", "true")
    d = evaluate_webull_order(notional_usd=2.00, symbol="AAPL")
    assert d.ok is True


def test_sub_one_dollar_still_blocked(monkeypatch):
    """The $1 floor is a hard floor — $0.99 intents stay blocked.
    Webull won't route them and we don't want dust orders consuming
    rate-limit budget."""
    monkeypatch.setenv("WEBULL_ARMED", "true")
    d = evaluate_webull_order(notional_usd=0.99, symbol="AAPL")
    assert d.ok is False
    assert "BELOW_FLOOR" in d.reason



# ── dynamic buying-power cap (2026-02-20) ──────────────────────────


def test_dynamic_cap_allows_25_when_bp_500(monkeypatch):
    """Operator's blocked case: $25 intent on AAPL with BP=$500.
    Default 5% pct × $500 = $25 ceiling → intent at exactly $25 passes."""
    monkeypatch.setenv("WEBULL_ARMED", "true")
    # Raise env cap so it doesn't bind — we want to verify BP cap wins.
    monkeypatch.setenv("WEBULL_MAX_NOTIONAL_USD", "500")
    d = evaluate_webull_order(
        notional_usd=25.00, symbol="AAPL", buying_power_usd=500.0,
    )
    assert d.ok is True
    assert d.cap_source == "buying_power"
    assert d.max_usd == 25.00


def test_dynamic_cap_blocks_above_bp_pct(monkeypatch):
    """5% of $500 = $25 ceiling. A $26 intent must be rejected and
    the reason must surface the BP source so the operator knows to
    fund the account or raise the pct."""
    monkeypatch.setenv("WEBULL_ARMED", "true")
    monkeypatch.setenv("WEBULL_MAX_NOTIONAL_USD", "500")
    d = evaluate_webull_order(
        notional_usd=26.00, symbol="AAPL", buying_power_usd=500.0,
    )
    assert d.ok is False
    assert "ABOVE_CAP" in d.reason
    assert "buying power" in d.reason  # operator hint surfaced
    assert d.cap_source == "buying_power"


def test_dynamic_cap_falls_back_when_bp_missing(monkeypatch):
    """If BP fetch fails (None), behave like the old env-only gate.
    Pre-2026-02-20 callers (no buying_power_usd) MUST keep working."""
    monkeypatch.setenv("WEBULL_ARMED", "true")
    d = evaluate_webull_order(notional_usd=5.00, symbol="AAPL")
    assert d.ok is True
    assert d.cap_source == "env"
    assert d.buying_power_usd is None


def test_dynamic_cap_falls_back_when_bp_zero(monkeypatch):
    """Zero BP should not collapse the ceiling to $0 — fall back to
    env so the gate refuses for the right reason (above-env-cap)
    rather than silently allowing $0."""
    monkeypatch.setenv("WEBULL_ARMED", "true")
    d = evaluate_webull_order(
        notional_usd=5.00, symbol="AAPL", buying_power_usd=0.0,
    )
    assert d.ok is True
    assert d.cap_source == "env"


def test_dynamic_cap_env_binds_when_smaller_than_bp_cap(monkeypatch):
    """Belt-and-suspenders: operator can pin a hard dollar ceiling via
    WEBULL_MAX_NOTIONAL_USD. BP cap = 5% × $10,000 = $500, but env
    says $20 → ceiling is $20, cap_source='env'."""
    monkeypatch.setenv("WEBULL_ARMED", "true")
    monkeypatch.setenv("WEBULL_MAX_NOTIONAL_USD", "20")
    d = evaluate_webull_order(
        notional_usd=25.00, symbol="AAPL", buying_power_usd=10_000.0,
    )
    assert d.ok is False
    assert d.cap_source == "env"
    assert d.max_usd == 20.0
