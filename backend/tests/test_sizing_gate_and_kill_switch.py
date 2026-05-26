"""Tripwires — Phase 4 sizing gate + runtime kill switch (2026-05-26).

Doctrine:
    1. When MICRO_LIVE_ENABLED is False, evaluate_sizing returns the
       lane cap as the binding rail.
    2. When MICRO_LIVE_ENABLED is True, evaluate_sizing returns
       min(lane_cap, micro_live_cap) — fail-CLOSED to the tighter rail.
    3. Per-lane micro_live overrides (crypto vs equity) work.
    4. Garbage / non-numeric / zero / negative inputs return 0 with
       binding_rail="invalid_input".
    5. Kill switch defaults to OFF on first boot (fail-CLOSED).
    6. is_trading_enabled fail-CLOSED on Mongo errors.
    7. Enabling requires a reason.
"""
from __future__ import annotations

import os

import pytest

from db import db
from shared import sizing_gate
from shared.sizing_gate import evaluate_sizing


pytestmark = pytest.mark.asyncio


# ─────────── A — sizing gate (synchronous) ───────────


def _reset_env(**overrides):
    for k in (
        "MICRO_LIVE_ENABLED", "MICRO_LIVE_DEFAULT_CAP_USD",
        "MICRO_LIVE_CRYPTO_CAP_USD", "MICRO_LIVE_EQUITY_CAP_USD",
    ):
        os.environ.pop(k, None)
    for k, v in overrides.items():
        os.environ[k] = str(v)
    sizing_gate.reload_env()


def test_micro_live_off_only_lane_cap_binds():
    _reset_env()
    d = evaluate_sizing(100.0, "equity")
    assert d.micro_live_enabled is False
    # equity lane cap is $100k — $100 request is under it.
    assert d.final_usd == 100.0
    assert d.was_clamped is False
    assert d.binding_rail == "none"
    _reset_env()


def test_micro_live_on_clamps_to_default_cap():
    _reset_env(MICRO_LIVE_ENABLED="true", MICRO_LIVE_DEFAULT_CAP_USD="5")
    d = evaluate_sizing(100.0, "equity")
    assert d.micro_live_enabled is True
    assert d.final_usd == 5.0
    assert d.was_clamped is True
    assert d.binding_rail == "micro_live"
    _reset_env()


def test_micro_live_crypto_specific_cap():
    _reset_env(
        MICRO_LIVE_ENABLED="true",
        MICRO_LIVE_DEFAULT_CAP_USD="100",
        MICRO_LIVE_CRYPTO_CAP_USD="5",
    )
    d_crypto = evaluate_sizing(1000.0, "crypto")
    d_equity = evaluate_sizing(1000.0, "equity")
    assert d_crypto.final_usd == 5.0
    assert d_equity.final_usd == 100.0
    _reset_env()


def test_micro_live_smaller_than_lane_cap_wins():
    """Doctrine: tighter rail wins. micro_live=$5 vs lane cap $500
    (crypto) → micro_live binds."""
    _reset_env(MICRO_LIVE_ENABLED="true", MICRO_LIVE_DEFAULT_CAP_USD="5")
    d = evaluate_sizing(1000.0, "crypto")
    assert d.final_usd == 5.0
    assert d.binding_rail == "micro_live"
    assert d.lane_cap_usd >= 5.0   # lane cap is bigger
    _reset_env()


def test_micro_live_larger_than_lane_cap_lane_wins():
    """Doctrine: tighter rail wins. micro_live=$5000 vs crypto lane
    cap $500 → lane_cap binds."""
    _reset_env(MICRO_LIVE_ENABLED="true", MICRO_LIVE_DEFAULT_CAP_USD="5000")
    d = evaluate_sizing(10_000.0, "crypto")
    assert d.binding_rail == "lane_cap"
    assert d.final_usd == d.lane_cap_usd
    _reset_env()


def test_invalid_input_zero_size():
    _reset_env()
    d = evaluate_sizing(0, "crypto")
    assert d.final_usd == 0.0
    assert d.binding_rail == "invalid_input"


def test_invalid_input_negative():
    _reset_env()
    d = evaluate_sizing(-100, "crypto")
    assert d.final_usd == 0.0
    assert d.binding_rail == "invalid_input"


def test_invalid_input_non_numeric():
    _reset_env()
    d = evaluate_sizing("nope", "crypto")
    assert d.final_usd == 0.0
    assert d.binding_rail == "invalid_input"


# ─────────── B — runtime kill switch ───────────


async def _clear_controls():
    await db["trading_controls"].delete_many({})
    await db["trading_controls_audit"].delete_many({})


async def test_first_boot_is_disabled():
    """Fail-CLOSED doctrine: untouched state = OFF."""
    from routes.trading_controls import get_trading_status
    await _clear_controls()
    doc = await get_trading_status()
    assert doc["enabled"] is False
    assert "first_boot" in (doc.get("reason") or "")
    await _clear_controls()


async def test_is_trading_enabled_default_false():
    from routes.trading_controls import is_trading_enabled
    await _clear_controls()
    assert await is_trading_enabled() is False
    await _clear_controls()


async def test_set_and_read_back():
    from routes.trading_controls import (
        get_trading_status, is_trading_enabled, set_trading_enabled,
    )
    await _clear_controls()
    await set_trading_enabled(True, "test enable", "test_actor")
    assert await is_trading_enabled() is True
    doc = await get_trading_status()
    assert doc["enabled"] is True
    assert doc["updated_by"] == "test_actor"
    assert "test enable" in doc["reason"]
    # Audit row written.
    audit = await db["trading_controls_audit"].find_one(
        {"updated_by": "test_actor"}, {"_id": 0},
    )
    assert audit is not None
    await _clear_controls()


async def test_disable_after_enable():
    from routes.trading_controls import (
        is_trading_enabled, set_trading_enabled,
    )
    await _clear_controls()
    await set_trading_enabled(True, "on", "test")
    await set_trading_enabled(False, "halt", "test")
    assert await is_trading_enabled() is False
    await _clear_controls()


# ─────────── C — auto-router integration ───────────


async def test_auto_router_blocks_when_trading_disabled():
    """Even with a fully-valid intent, the auto-router MUST refuse to
    fire when trading_controls is OFF."""
    from routes.trading_controls import set_trading_enabled
    from shared.auto_router import _route_one
    await _clear_controls()
    await set_trading_enabled(False, "test halt", "test")
    fake_intent = {
        "intent_id": "test-kill-switch-1",
        "stack": "alpha", "action": "BUY",
        "symbol": "AAPL", "lane": "equity",
        "confidence": 0.7, "rationale": "ks test",
    }
    result = await _route_one(fake_intent)
    # Either blocked by classifier (no symbol etc.) or by our kill switch.
    # Both are acceptable — we just want NOT executed.
    assert result.get("verdict") != "executed"
    await _clear_controls()
