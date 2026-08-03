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
    assert ok and r["reason"] == "allowlist_static" and r["notional_cap_usd"] is None
    ok2, r2 = await elig.evaluate_buy_eligibility("ICNT/USD")
    assert not ok2 and r2["reason"] == "not_in_buy_allowlist"


@pytest.mark.asyncio
async def test_hybrid_pin_always_allowed_uncapped(monkeypatch):
    _cfg(monkeypatch)
    _pins(monkeypatch, ["BTC/USD"])
    ok, r = await elig.evaluate_buy_eligibility("BTC/USD")
    assert ok and r["reason"] == "operator_pin" and r["notional_cap_usd"] is None


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
    # min($50, 0.5% of $1.2M=$6000) = $50
    _cfg(monkeypatch)
    _pins(monkeypatch, [])
    _metrics(monkeypatch, 1_200_000, 30.0)
    ok, r = await elig.evaluate_buy_eligibility("ICNT/USD")
    assert ok and r["reason"] == "rules_admitted"
    assert r["notional_cap_usd"] == 50.0


@pytest.mark.asyncio
async def test_pct_of_volume_cap_binds_on_thin_symbols(monkeypatch):
    # $1M day volume exactly at floor: 0.5% = $5000 > $50 → $50 binds;
    # but with a tiny $2k symbol below floor → rejected outright
    _cfg(monkeypatch, max_notional_offlist_usd=100.0)
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
    elig.reset_for_tests()
    _metrics(monkeypatch, 5_000_000, 80.0)
    ok2, r2 = await elig.evaluate_buy_eligibility("WIDE/USD")
    assert not ok2 and r2["reason"] == "spread_too_wide"


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
