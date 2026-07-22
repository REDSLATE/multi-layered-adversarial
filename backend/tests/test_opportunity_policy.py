"""Opportunity policy Phase 1 — tiers, authority windows, Rise
Kernel throttle (2026-07-22 operator aggression doctrine)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, "/app/backend")

from shared.opportunity.policy import (
    DEFAULTS, classify_tier, get_opportunity_policy, invalidate_policy_cache,
)
from shared.brains import kernel_throttle as kt


def _policy():
    import copy
    return copy.deepcopy(DEFAULTS)


# ── tier classification (operator spec) ─────────────────────────────

def test_tier_bands_equity():
    p = _policy()
    assert classify_tier(0.20, "equity", p) == ("WATCH", 0.0)
    assert classify_tier(0.32, "equity", p) == ("PROBE", 5.0)
    assert classify_tier(0.39, "equity", p) == ("PROBE", 5.0)
    assert classify_tier(0.40, "equity", p) == ("ENTER", 7.5)
    assert classify_tier(0.61, "equity", p) == ("ENTER", 7.5)
    assert classify_tier(0.62, "equity", p) == ("FULL", 10.0)


def test_crypto_enter_threshold_is_lower():
    p = _policy()
    # 0.37 is ENTER on crypto (threshold 0.35) but PROBE on equity (0.40)
    assert classify_tier(0.37, "crypto", p)[0] == "ENTER"
    assert classify_tier(0.37, "equity", p)[0] == "PROBE"


def test_defaults_match_operator_config():
    assert DEFAULTS["tiers"]["equity"] == {"probe": 0.32, "enter": 0.40, "press": 0.62}
    assert DEFAULTS["authority_min"] == {"equity": 15.0, "crypto": 30.0}
    assert DEFAULTS["kernel"]["min_mult"] == 0.50
    assert DEFAULTS["kernel"]["max_mult"] == 1.35


@pytest.mark.asyncio
async def test_policy_overrides_merge_from_runtime_flags():
    from db import db
    await db["runtime_flags"].delete_one({"_id": "opportunity_policy"})
    invalidate_policy_cache()
    try:
        await db["runtime_flags"].update_one(
            {"_id": "opportunity_policy"},
            {"$set": {"tiers.crypto.enter": 0.38, "authority_min.crypto": 20}},
            upsert=True,
        )
        invalidate_policy_cache()
        p = await get_opportunity_policy()
        assert p["tiers"]["crypto"]["enter"] == 0.38
        assert p["authority_min"]["crypto"] == 20.0
        assert p["tiers"]["equity"]["enter"] == 0.40  # untouched default
    finally:
        await db["runtime_flags"].delete_one({"_id": "opportunity_policy"})
        invalidate_policy_cache()


# ── kernel throttle ─────────────────────────────────────────────────

async def _seed_outcomes(db, brain, pnls):
    now = datetime.now(timezone.utc)
    rows = [
        {"symbol": "KTHR/USD", "lane": "crypto", "brain": brain,
         "outcome": "tp_hit" if p > 0 else "sl_hit",
         "realized_pnl_pct": p, "realized_pnl_usd": p / 10,
         "closed_at": (now - timedelta(minutes=i)).isoformat(),
         "plan_id": f"kt-{brain}-{i}"}
        for i, p in enumerate(pnls)
    ]
    await db[kt.EXIT_OUTCOMES].insert_many(rows)


@pytest.mark.asyncio
async def test_cold_start_is_neutral():
    kt.invalidate_kernel_cache()
    with patch.object(kt, "performance_from_outcomes", new=AsyncMock(return_value=None)):
        out = await kt.get_kernel_throttle("nobrain", "crypto")
    assert out["multiplier"] == 1.0
    assert out["state"] == "cold_start_neutral"


@pytest.mark.asyncio
async def test_hot_brain_scales_up_cold_brain_scales_down():
    from db import db
    await db[kt.EXIT_OUTCOMES].delete_many({"symbol": "KTHR/USD"})
    kt.invalidate_kernel_cache()
    try:
        await _seed_outcomes(db, "hotb", [8.0, 7.5, 6.0, 8.2, 5.0, 7.0])
        await _seed_outcomes(db, "coldb", [-3.0, -3.1, -2.9, -3.0, -3.2, -2.8])
        hot = await kt.get_kernel_throttle("hotb", "crypto")
        cold = await kt.get_kernel_throttle("coldb", "crypto")
        assert hot["state"] == "live" and cold["state"] == "live"
        assert hot["multiplier"] > cold["multiplier"]
        assert 0.50 <= cold["multiplier"] <= 1.35
        assert 0.50 <= hot["multiplier"] <= 1.35
        assert hot["multiplier"] > 1.0, "winning brain must be elevated"
        assert cold["multiplier"] < 1.0, "losing brain must be throttled"
    finally:
        await db[kt.EXIT_OUTCOMES].delete_many({"symbol": "KTHR/USD"})
        kt.invalidate_kernel_cache()


@pytest.mark.asyncio
async def test_kernel_disabled_is_neutral():
    from db import db
    kt.invalidate_kernel_cache()
    await db["runtime_flags"].update_one(
        {"_id": "opportunity_policy"},
        {"$set": {"kernel.enabled": False}}, upsert=True,
    )
    invalidate_policy_cache()
    try:
        out = await kt.get_kernel_throttle("gto", "equity")
        assert out == {"multiplier": 1.0, "score": None,
                       "state": "disabled", "trades": 0}
    finally:
        await db["runtime_flags"].delete_one({"_id": "opportunity_policy"})
        invalidate_policy_cache()
        kt.invalidate_kernel_cache()


# ── router gate: authority + tiers (integration, mocked seat) ──────

def _mk_ctx(intent):
    from shared.auto_router_helpers import RouteContext
    return RouteContext(intent=dict(intent), intent_id=intent["intent_id"])


@pytest.mark.asyncio
async def test_stale_intent_blocked_by_authority_window():
    from db import db
    from shared.auto_router_stages import _gate_seat
    intent = {
        "intent_id": "opp-auth-1", "lane": "crypto", "action": "BUY",
        "symbol": "BTC/USD", "confidence": 0.9,
        "ingest_ts": (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(),
    }
    await db["shared_intents"].delete_many({"intent_id": intent["intent_id"]})
    await db["shared_intents"].insert_one(dict(intent))
    invalidate_policy_cache()
    try:
        res = await _gate_seat(_mk_ctx(intent))
        assert res is not None and res["reason"] == "authority_expired"
        doc = await db["shared_intents"].find_one({"intent_id": intent["intent_id"]})
        assert doc["gate_state"] == "expired_unrouted"
        assert doc["broker_reason"] == "AUTHORITY_EXPIRED"
    finally:
        await db["shared_intents"].delete_many({"intent_id": intent["intent_id"]})


@pytest.mark.asyncio
async def test_low_conviction_becomes_watch_no_capital():
    from db import db
    from shared.auto_router_stages import _gate_seat
    intent = {
        "intent_id": "opp-watch-1", "lane": "crypto", "action": "BUY",
        "symbol": "BTC/USD", "confidence": 0.20,
        "ingest_ts": datetime.now(timezone.utc).isoformat(),
    }
    await db["shared_intents"].delete_many({"intent_id": intent["intent_id"]})
    await db["shared_intents"].insert_one(dict(intent))
    invalidate_policy_cache()
    try:
        res = await _gate_seat(_mk_ctx(intent))
        assert res is not None and res["reason"] == "below_probe_threshold"
        doc = await db["shared_intents"].find_one({"intent_id": intent["intent_id"]})
        assert doc["action_tier"] == "WATCH"
        assert doc["broker_reason"] == "BELOW_PROBE_THRESHOLD"
    finally:
        await db["shared_intents"].delete_many({"intent_id": intent["intent_id"]})


@pytest.mark.asyncio
async def test_fresh_probe_intent_gets_tier_notional():
    from db import db
    from shared.auto_router_stages import _gate_seat
    from shared import seat as seat_mod
    intent = {
        "intent_id": "opp-probe-1", "lane": "crypto", "action": "BUY",
        "symbol": "BTC/USD", "confidence": 0.33,
        "ingest_ts": datetime.now(timezone.utc).isoformat(),
    }
    await db["shared_intents"].delete_many({"intent_id": intent["intent_id"]})
    await db["shared_intents"].insert_one(dict(intent))
    invalidate_policy_cache()
    ctx = _mk_ctx(intent)

    class _SD:
        verdict = "fire"
        executor = "gto"
        reason = "ok"
        risk_multiplier = 1.0
        intent_brain = "gto"
        lane = "crypto"

    try:
        with patch.object(seat_mod, "decide", new=AsyncMock(return_value=_SD())):
            res = await _gate_seat(ctx)
        assert res is None, "fire verdict passes through"
        assert ctx.notional_raw == 5.0
        assert ctx.notional_source == "tier_probe"
        doc = await db["shared_intents"].find_one({"intent_id": intent["intent_id"]})
        assert doc["action_tier"] == "PROBE"
    finally:
        await db["shared_intents"].delete_many({"intent_id": intent["intent_id"]})
