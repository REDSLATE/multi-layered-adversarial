"""Options broker adapter + chain feed + lane routing.

Covers: contract resolution (delta targeting, DTE window, quality-gate
rejections, fail-closed on missing client/quotes), the Webull
place_option payload shape (LIMIT-only doctrine, leg fields, ARMED
gate), and lane plumbing (OPT canonical, webull registry, cap-gate
bypass, contract-field validation).
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.broker_symbol_resolver import LANE_BROKER_REGISTRY, broker_for_lane, compose
from shared.options import chain
from shared.risk_sizer import policy as sizer_policy

OPT_POL = dict(sizer_policy.DEFAULTS["options"])


# ─────────────────────── chain resolution ────────────────────────

def _chain_row(strike, expiration="2026-08-21", opt_type="CALL"):
    return {"symbol": f"AAPL{expiration.replace('-', '')[2:]}"
                      f"{'C' if opt_type == 'CALL' else 'P'}"
                      f"{int(strike * 1000):08d}",
            "option_type": opt_type, "tradable_status": "OC",
            "expiration_date": expiration, "strike_price": str(strike),
            "multiplier": "100", "status": "LISTING",
            "instrument_id": str(strike)}


def _snap(symbol, *, bid, ask, delta, theta=-0.005, oi=500):
    return {"symbol": symbol, "bid": str(bid), "ask": str(ask),
            "delta": str(delta), "theta": str(theta),
            "open_interest": str(oi), "gamma": "0.01", "imp_vol": "0.3"}


@pytest.fixture
def wired_chain(monkeypatch):
    chain.reset_for_tests()
    from datetime import date, timedelta
    exp = (date.today() + timedelta(days=30)).isoformat()
    rows = [_chain_row(s, exp) for s in (190, 195, 200, 205, 210)]
    rows += [_chain_row(s, exp, "PUT") for s in (190, 200)]
    # expired + too-far contracts must be filtered out
    rows.append(_chain_row(200, (date.today() + timedelta(days=2)).isoformat()))
    rows.append(_chain_row(200, (date.today() + timedelta(days=120)).isoformat()))
    snaps = {
        rows[0]["symbol"]: _snap(rows[0]["symbol"], bid=12.0, ask=12.4, delta=0.72),
        rows[1]["symbol"]: _snap(rows[1]["symbol"], bid=8.0, ask=8.3, delta=0.63),
        rows[2]["symbol"]: _snap(rows[2]["symbol"], bid=5.0, ask=5.2, delta=0.51),
        rows[3]["symbol"]: _snap(rows[3]["symbol"], bid=3.0, ask=3.15, delta=0.38),
        rows[4]["symbol"]: _snap(rows[4]["symbol"], bid=1.8, ask=1.9, delta=0.27),
    }
    monkeypatch.setattr(chain, "_fetch_chain_rows", lambda u: list(rows))
    monkeypatch.setattr(chain, "_fetch_snapshots",
                        lambda syms: [snaps[s] for s in syms if s in snaps])
    monkeypatch.setattr(chain, "_fetch_spot", lambda u: 200.0)
    yield exp, rows, snaps
    chain.reset_for_tests()


@pytest.mark.asyncio
async def test_resolve_picks_delta_closest_to_target(wired_chain):
    res = await chain.resolve_contract("AAPL", "BUY", OPT_POL)
    assert res["reason"] == "resolved"
    c = res["contract"]
    # target |delta| 0.50 → the 200-strike 0.51-delta call
    assert c["strike_price"] == 200
    assert c["delta"] == pytest.approx(0.51)
    assert c["premium"] == pytest.approx(5.1)   # mid of 5.0/5.2
    assert c["option_type"] == "CALL"
    assert c["multiplier"] == 100.0
    assert OPT_POL["min_dte"] <= c["dte"] <= OPT_POL["max_dte"]


@pytest.mark.asyncio
async def test_resolve_short_uses_puts(wired_chain):
    res = await chain.resolve_contract("AAPL", "SHORT", OPT_POL)
    # puts exist in the chain but have no snapshots wired → quality fail
    assert res["contract"] is None
    assert res["reason"] == "no_contract_passes_quality_gates"


@pytest.mark.asyncio
async def test_resolve_rejections_carry_gate_reasons(wired_chain, monkeypatch):
    exp, rows, snaps = wired_chain
    for s in snaps.values():
        s["open_interest"] = "5"   # below min 100 → every candidate rejected
    res = await chain.resolve_contract("AAPL", "BUY", OPT_POL)
    assert res["contract"] is None
    assert all(r["reason"] == "options_open_interest_too_low"
               for r in res["rejections"] if r["reason"] != "no_snapshot")


@pytest.mark.asyncio
async def test_resolve_fails_closed_without_client(monkeypatch):
    chain.reset_for_tests()

    def boom(u):
        raise RuntimeError("no Webull quotes client (credentials missing)")
    monkeypatch.setattr(chain, "_fetch_chain_rows", boom)
    res = await chain.resolve_contract("AAPL", "BUY", OPT_POL)
    assert res["contract"] is None
    assert "chain_fetch_failed" in res["reason"]


@pytest.mark.asyncio
async def test_resolve_fails_closed_without_spot(wired_chain, monkeypatch):
    monkeypatch.setattr(chain, "_fetch_spot", lambda u: None)
    res = await chain.resolve_contract("AAPL", "BUY", OPT_POL)
    assert res["contract"] is None
    assert res["reason"] == "no_underlying_quote"


# ─────────────────── adapter payload (place_option) ──────────────

@pytest.mark.asyncio
async def test_submit_option_limit_order_payload(monkeypatch):
    from shared.broker.webull import WebullAdapter
    monkeypatch.setenv("WEBULL_ARMED", "true")
    adapter = WebullAdapter(api_client=None, account_id="acct-1")
    captured = {}

    async def fake_sdk_call(fn, *args, **kwargs):
        captured["fn"] = getattr(fn, "__name__", str(fn))
        captured["args"] = args
        return {"code": "200", "data": [{"orderId": "wb-42", "status": "SUBMITTED"}]}

    monkeypatch.setattr(adapter, "_sdk_call", fake_sdk_call)
    monkeypatch.setattr(adapter, "_trade", lambda: type(
        "T", (), {"order_v2": type("O", (), {"place_option": lambda *a: None})()})())

    async def fake_account_id():
        return "acct-1"
    monkeypatch.setattr(adapter, "_resolve_account_id", fake_account_id)

    order = await adapter.submit_option_limit_order(
        underlying="aapl", option_type="CALL", strike_price=200.0,
        expire_date="2026-08-21", contracts=2, limit_price=5.20,
        side="BUY", client_order_id="opt-test-123",
    )
    account_id, new_orders = captured["args"]
    assert account_id == "acct-1"
    o = new_orders[0]
    assert o["order_type"] == "LIMIT"           # MARKET prohibited for options
    assert o["option_strategy"] == "SINGLE"
    assert o["instrument_type"] == "OPTION"
    assert o["entrust_type"] == "QTY"
    assert o["time_in_force"] == "DAY"
    assert o["quantity"] == "2"
    assert o["limit_price"] == "5.20"
    assert o["symbol"] == "AAPL"
    leg = o["legs"][0]
    assert leg["option_type"] == "CALL"
    assert leg["strike_price"] == "200.00"
    assert leg["option_expire_date"] == "2026-08-21"
    assert leg["market"] == "US"
    assert order["order_id"] == "wb-42"
    assert order["contracts"] == 2
    assert order["notional"] == pytest.approx(1040.0)


@pytest.mark.asyncio
async def test_submit_option_blocked_when_not_armed(monkeypatch):
    from shared.broker.webull import WebullAdapter
    from shared.broker.webull_caps import WebullCapBlocked
    monkeypatch.setenv("WEBULL_ARMED", "false")
    adapter = WebullAdapter(api_client=None)
    with pytest.raises(WebullCapBlocked):
        await adapter.submit_option_limit_order(
            underlying="AAPL", option_type="CALL", strike_price=200.0,
            expire_date="2026-08-21", contracts=1, limit_price=5.0)


# ───────────────────── lane routing plumbing ─────────────────────

def test_options_canonical_and_registry():
    asset = compose("AAPL", "options")
    assert asset.canonical == "OPT:AAPL"
    assert asset.lane == "options"
    assert asset.base == "AAPL"
    assert broker_for_lane("options") == "webull"
    assert LANE_BROKER_REGISTRY["options"] == "webull"


def test_compose_asset_parses_opt_canonical():
    from shared.broker_router import compose_asset
    asset = compose_asset({"canonical": "OPT:TSLA"})
    assert asset.lane == "options"
    assert asset.base == "TSLA"


def test_mc_canonical_gate_accepts_options_lane():
    from shared.runtime.platform_survival import mc_canonical_gate, policy_hash
    result = mc_canonical_gate({
        "runtime": {"local_execution_authority": False,
                    "policy_hash": policy_hash()},
        "direction": "BUY", "confidence": 0.7,
        "lane": "options", "symbol": "AAPL",
    })
    assert "BAD_LANE" not in (result.get("errors") or [])
    assert result["accepted"] is True
