"""Hybrid BUY eligibility tests (2026-08-03 operator directive)."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.risk_sizer import buy_eligibility as elig  # noqa: E402

pytestmark = pytest.mark.tripwire


@pytest.fixture(autouse=True)
def _fresh():
    elig.reset_for_tests()
    yield
    elig.reset_for_tests()


def _cfg(monkeypatch, **over):
    async def _get():
        return {**elig.DEFAULTS, **over}
    monkeypatch.setattr(elig, "get_eligibility_config", _get)


def _metrics(monkeypatch, dvol, spread):
    async def _dv(sym):
        return dvol
    async def _sp(sym):
        return spread
    monkeypatch.setattr(elig, "_dollar_volume_24h", _dv)
    monkeypatch.setattr(elig, "_spread_bps", _sp)


def _pins(monkeypatch, symbols):
    from shared.risk_sizer import buy_allowlist as al
    async def _get():
        return {"enabled": True, "symbols": symbols}
    monkeypatch.setattr(al, "get_allowlist", _get)


@pytest.mark.asyncio
async def test_static_mode_defers_to_legacy_allowlist(monkeypatch):
    _cfg(monkeypatch, mode="static")
    from shared.risk_sizer import buy_allowlist as al
    async def _buy_allowed(sym):
        return sym == "BTC/USD", {"symbols": ["BTC/USD"]}
    monkeypatch.setattr(al, "buy_allowed", _buy_allowed)
    ok, r = await elig.evaluate_buy_eligibility("BTC/USD")
    assert ok and r["reason"] == "allowlist_static" and r["notional_cap_usd"] == 5.0
    ok2, r2 = await elig.evaluate_buy_eligibility("ICNT/USD")
    assert not ok2 and r2["reason"] == "not_in_buy_allowlist"
    assert r2["notional_cap_usd"] is None


@pytest.mark.asyncio
async def test_hybrid_pin_allowed_with_per_trade_cap(monkeypatch):
    # 2026-08-03 operator: $5/trade applies to ALL crypto BUYs, pins too
    _cfg(monkeypatch)
    _pins(monkeypatch, ["BTC/USD"])
    ok, r = await elig.evaluate_buy_eligibility("BTC/USD")
    assert ok and r["reason"] == "operator_pin" and r["notional_cap_usd"] == 5.0


@pytest.mark.asyncio
async def test_cap_knob_adjustable_reflects_on_pins(monkeypatch):
    _cfg(monkeypatch, max_notional_usd=12.5)
    _pins(monkeypatch, ["BTC/USD"])
    ok, r = await elig.evaluate_buy_eligibility("BTC/USD")
    assert ok and r["notional_cap_usd"] == 12.5


@pytest.mark.asyncio
async def test_denylist_blocks_even_liquid_symbols(monkeypatch):
    _cfg(monkeypatch, denylist=["SCAM/USD"])
    _pins(monkeypatch, [])
    _metrics(monkeypatch, 50_000_000, 5.0)
    ok, r = await elig.evaluate_buy_eligibility("SCAMUSD")  # normalization too
    assert not ok and r["reason"] == "denylisted"


@pytest.mark.asyncio
async def test_rules_admit_icnt_class_with_notional_cap(monkeypatch):
    # ICNT-like: $1.2M day volume, 30bps spread → admitted, capped at
    # min($5, 0.5% of $1.2M=$6000) = $5
    _cfg(monkeypatch)
    _pins(monkeypatch, [])
    _metrics(monkeypatch, 1_200_000, 30.0)
    ok, r = await elig.evaluate_buy_eligibility("ICNT/USD")
    assert ok and r["reason"] == "rules_admitted"
    assert r["notional_cap_usd"] == 5.0


@pytest.mark.asyncio
async def test_pct_of_volume_cap_binds_on_thin_symbols(monkeypatch):
    # raised knob $100 with $1M day volume: 0.5% = $5000 → $100 binds
    _cfg(monkeypatch, max_notional_usd=100.0)
    _pins(monkeypatch, [])
    _metrics(monkeypatch, 1_000_000, 10.0)
    ok, r = await elig.evaluate_buy_eligibility("THIN/USD")
    assert ok and r["notional_cap_usd"] == 100.0


@pytest.mark.asyncio
async def test_volume_floor_and_spread_cap_reject(monkeypatch):
    _cfg(monkeypatch)
    _pins(monkeypatch, [])
    _metrics(monkeypatch, 400_000, 10.0)
    ok, r = await elig.evaluate_buy_eligibility("TINY/USD")
    assert not ok and r["reason"] == "below_volume_floor"
    # 2026 MC directive: wide (but not extreme) spread is execution
    # friction — ADMITTED with a ladder flag, never returned to HOLD.
    elig.reset_for_tests()
    _metrics(monkeypatch, 5_000_000, 80.0)
    ok2, r2 = await elig.evaluate_buy_eligibility("WIDE/USD")
    assert ok2 and r2["reason"] == "wide_spread_ladder"
    assert r2["execution_friction"] == "spread_too_wide"
    assert r2["notional_cap_usd"] == 5.0
    # extreme/broken book still hard-rejects
    elig.reset_for_tests()
    _metrics(monkeypatch, 5_000_000, 350.0)
    ok3, r3 = await elig.evaluate_buy_eligibility("BROKEN/USD")
    assert not ok3 and r3["reason"] == "spread_extreme"


@pytest.mark.asyncio
async def test_dynamic_fails_closed_on_missing_quote(monkeypatch):
    _cfg(monkeypatch)
    _pins(monkeypatch, [])
    _metrics(monkeypatch, 5_000_000, None)
    ok, r = await elig.evaluate_buy_eligibility("NOQ/USD")
    assert not ok and r["reason"] == "no_quote"


def test_sizer_wiring():
    src = open("/app/backend/shared/risk_sizer/sizer.py").read()
    assert "evaluate_buy_eligibility" in src
    assert "_elig_cap" in src
    assert "eligibility_cap_below_broker_min" in src
    routes = open("/app/backend/routes/universe_admin.py").read()
    assert '"/buy-eligibility"' in routes and '"/buy-eligibility/probe"' in routes
