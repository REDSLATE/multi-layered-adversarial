"""Exit Monitor core invariants (2026-07-22)."""
from __future__ import annotations

import sys
from datetime import timedelta

import pytest

sys.path.insert(0, "/app/backend")

from shared.exits import monitor as em
from shared.exits.policy import DEFAULTS


def _plan(**over):
    base = {
        "plan_id": "p1", "lane": "crypto", "symbol": "BTC/USD",
        "status": "active", "entry_price": 100.0,
        "stop_price": 97.0, "target_price": 108.0,
        "qty_held": 0.5, "max_hold_until": em._iso(em._now() + timedelta(hours=1)),
        "attempts": 0,
    }
    base.update(over)
    return base


# ── trigger evaluation ──────────────────────────────────────────────

def test_stop_loss_fires_at_or_below_stop():
    assert em._trigger_for(_plan(), 97.0) == "stop_loss"
    assert em._trigger_for(_plan(), 96.0) == "stop_loss"


def test_take_profit_fires_at_or_above_target():
    assert em._trigger_for(_plan(), 108.0) == "take_profit"
    assert em._trigger_for(_plan(), 120.0) == "take_profit"


def test_hold_zone_no_trigger():
    assert em._trigger_for(_plan(), 100.0) is None
    assert em._trigger_for(_plan(), 107.99) is None
    assert em._trigger_for(_plan(), 97.01) is None


def test_max_hold_expiry_triggers_even_in_hold_zone():
    expired = _plan(max_hold_until=em._iso(em._now() - timedelta(minutes=1)))
    assert em._trigger_for(expired, 100.0) == "max_hold"


def test_stop_loss_beats_max_hold_priority():
    expired = _plan(max_hold_until=em._iso(em._now() - timedelta(minutes=1)))
    assert em._trigger_for(expired, 90.0) == "stop_loss"


# ── kraken balance normalization ────────────────────────────────────

def test_kraken_legacy_asset_codes_normalize():
    assert em._normalize_kraken_asset("XXBT") == "BTC"
    assert em._normalize_kraken_asset("XETH") == "ETH"
    assert em._normalize_kraken_asset("XXDG") == "DOGE"
    assert em._normalize_kraken_asset("SOL") == "SOL"


def test_kraken_cash_and_staking_assets_excluded():
    assert em._normalize_kraken_asset("ZUSD") is None
    assert em._normalize_kraken_asset("USDT") is None
    assert em._normalize_kraken_asset("ETH.S") == "ETH"


# ── policy defaults (operator spec) ─────────────────────────────────

def test_lane_defaults_match_operator_spec():
    assert DEFAULTS["equity"] == {
        "enabled": False, "sl_pct": 3.0, "tp_pct": 6.0, "max_hold_h": 24.0,
    }
    assert DEFAULTS["crypto"] == {
        "enabled": False, "sl_pct": 3.0, "tp_pct": 8.0, "max_hold_h": 48.0,
    }


# ── atomic reservation (live db) ────────────────────────────────────

@pytest.mark.asyncio
async def test_reservation_is_atomic_and_single_winner():
    from db import db
    plan = _plan(plan_id="test-reserve-atomic")
    await db[em.EXIT_PLANS].delete_many({"plan_id": plan["plan_id"]})
    await db[em.EXIT_PLANS].insert_one(dict(plan))
    try:
        first = await em._reserve(plan["plan_id"], "stop_loss")
        second = await em._reserve(plan["plan_id"], "take_profit")
        assert first is True
        assert second is False, "second reservation must lose"
        doc = await db[em.EXIT_PLANS].find_one({"plan_id": plan["plan_id"]})
        assert doc["status"] == "exiting"
        assert doc["exit_reason"] == "stop_loss"
    finally:
        await db[em.EXIT_PLANS].delete_many({"plan_id": plan["plan_id"]})


@pytest.mark.asyncio
async def test_adoption_uses_brain_levels_only_when_coherent():
    """Incoherent brain bracket (stop above entry) falls back to lane
    defaults."""
    from unittest.mock import AsyncMock, patch
    from db import db
    pos = {"symbol": "TEST/USD", "qty": 1.0, "entry_price": 100.0,
           "current_price": 100.0}
    policy = {
        "equity": dict(DEFAULTS["equity"]),
        "crypto": dict(DEFAULTS["crypto"]),
        "escalate_after_s": 120.0,
    }
    await db[em.EXIT_PLANS].delete_many({"symbol": "TEST/USD"})
    try:
        with patch.object(em, "_brain_levels", new=AsyncMock(return_value=(95.0, 110.0))):
            plan = await em._adopt("crypto", pos, policy)
        assert plan["levels_source"] == "lane_default"
        assert plan["stop_price"] == pytest.approx(97.0)
        assert plan["target_price"] == pytest.approx(108.0)

        await db[em.EXIT_PLANS].delete_many({"symbol": "TEST/USD"})
        with patch.object(em, "_brain_levels", new=AsyncMock(return_value=(112.0, 96.0))):
            plan = await em._adopt("crypto", pos, policy)
        assert plan["levels_source"] == "brain"
        assert plan["stop_price"] == 96.0
        assert plan["target_price"] == 112.0
    finally:
        await db[em.EXIT_PLANS].delete_many({"symbol": "TEST/USD"})
