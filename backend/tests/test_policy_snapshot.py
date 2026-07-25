"""ExecutionPolicySnapshot — hot-path audit P1 #3/#4 (2026-07-24).

Pins: env defaults when nothing exists, Atlas refresh mapping, SQLite
recovery after restart, write-through apply_local, freeze/lane/cap
gates on the risk check, conviction-floor + opportunity-policy
accessors going through the snapshot.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.hotpath import daily_spend, intent_queue, outbox, policy_snapshot

_FLAG_IDS = ["risk_caps", "master_trading_switch", "lane_enabled",
             "conviction_floor", "opportunity_policy", "daily_spend_reset"]


@pytest.fixture
def hp(tmp_path):
    outbox.reset_for_tests(str(tmp_path / "hp.sqlite"))
    policy_snapshot.reset_for_tests()
    daily_spend.reset_for_tests()
    intent_queue.reset_for_tests()
    yield
    import os
    outbox.reset_for_tests(os.environ.get("HOTPATH_DB_PATH", "/app/backend/data/hotpath.sqlite"))
    policy_snapshot.reset_for_tests()
    daily_spend.reset_for_tests()
    intent_queue.reset_for_tests()


async def _snapshot_flags(db) -> dict:
    out = {}
    for fid in _FLAG_IDS:
        out[fid] = await db["runtime_flags"].find_one({"_id": fid})
    return out


async def _restore_flags(db, saved: dict) -> None:
    for fid, doc in saved.items():
        if doc is None:
            await db["runtime_flags"].delete_one({"_id": fid})
        else:
            await db["runtime_flags"].replace_one({"_id": fid}, doc, upsert=True)


def test_defaults_without_atlas_or_sqlite(hp):
    snap = policy_snapshot.get()
    assert snap["source"] == "defaults"
    assert snap["master_switch_enabled"] is True
    assert snap["lane_enabled"] == {"equity": True, "crypto": True}
    assert snap["broker_frozen"] is False
    assert snap["cap_daily_usd_override"] is None
    assert policy_snapshot.is_freeze_on() is False
    assert policy_snapshot.is_lane_enabled("crypto") is True


def test_apply_local_bumps_version_and_persists(hp):
    v0 = policy_snapshot.get().get("version", 0)
    policy_snapshot.apply_local(broker_frozen=True, broker_freeze_reason="test")
    snap = policy_snapshot.get()
    assert snap["version"] == v0 + 1
    assert policy_snapshot.is_broker_frozen() is True
    # Simulated restart: memory wiped, SQLite recovers the state.
    policy_snapshot._snap = None  # noqa: SLF001
    recovered = policy_snapshot.get()
    assert recovered["broker_frozen"] is True
    assert recovered["source"] == "sqlite"


@pytest.mark.asyncio
async def test_refresh_from_atlas_maps_all_flags(hp):
    from db import db
    saved = await _snapshot_flags(db)
    try:
        await db["runtime_flags"].replace_one(
            {"_id": "risk_caps"}, {"_id": "risk_caps", "cap_daily_usd": 777.0},
            upsert=True)
        await db["runtime_flags"].replace_one(
            {"_id": "master_trading_switch"},
            {"_id": "master_trading_switch", "enabled": False}, upsert=True)
        await db["runtime_flags"].replace_one(
            {"_id": "lane_enabled"},
            {"_id": "lane_enabled", "equity": False, "crypto": True}, upsert=True)
        await db["runtime_flags"].replace_one(
            {"_id": "conviction_floor"},
            {"_id": "conviction_floor", "value": 0.4}, upsert=True)
        await db["runtime_flags"].replace_one(
            {"_id": "opportunity_policy"},
            {"_id": "opportunity_policy", "tiers": {"crypto": {"enter": 0.38}}},
            upsert=True)
        snap = await policy_snapshot.refresh_from_atlas()
        assert snap["cap_daily_usd_override"] == 777.0
        assert snap["master_switch_enabled"] is False
        assert snap["lane_enabled"] == {"equity": False, "crypto": True}
        assert snap["conviction_floor"] == 0.4
        assert snap["opportunity_policy"]["tiers"]["crypto"]["enter"] == 0.38
        assert policy_snapshot.effective_daily_cap() == 777.0
        assert policy_snapshot.is_freeze_on() is True
        assert policy_snapshot.is_lane_enabled("equity") is False
    finally:
        await _restore_flags(db, saved)


@pytest.mark.asyncio
async def test_risk_check_gates_read_snapshot(hp, monkeypatch):
    from shared.risk.check import check
    monkeypatch.setenv("RISEDUAL_CAP_DAILY_USD", "1000")
    policy_snapshot._dirty = False  # noqa: SLF001
    intent = {"intent_id": "snap-gate-1", "lane": "crypto"}

    policy_snapshot.apply_local(master_switch_enabled=False)
    r = await check(intent, notional_usd=5.0)
    assert r.ok is False and r.reason == "master_freeze_on"

    policy_snapshot.apply_local(
        master_switch_enabled=True,
        lane_enabled={"equity": True, "crypto": False},
    )
    r = await check(intent, notional_usd=5.0)
    assert r.ok is False and r.reason == "lane_disabled:crypto"

    policy_snapshot.apply_local(lane_enabled={"equity": True, "crypto": True})
    r = await check(intent, notional_usd=5.0)
    assert r.ok is True


@pytest.mark.asyncio
async def test_conviction_floor_accessor_via_snapshot(hp, monkeypatch):
    from shared.auto_router_stages import (
        get_conviction_floor, peek_conviction_floor,
    )
    monkeypatch.setenv("AUTO_ROUTER_MIN_CONVICTION_MULT", "0.25")
    policy_snapshot._dirty = False  # noqa: SLF001
    policy_snapshot.apply_local(conviction_floor=None)
    assert peek_conviction_floor() == 0.25
    assert await get_conviction_floor() == 0.25
    policy_snapshot.apply_local(conviction_floor=0.6)
    assert peek_conviction_floor() == 0.6
    assert await get_conviction_floor() == 0.6


@pytest.mark.asyncio
async def test_broker_freeze_write_through(hp):
    from shared.broker_freeze import BrokerFrozen, assert_not_frozen, is_frozen
    policy_snapshot._dirty = False  # noqa: SLF001
    policy_snapshot.apply_local(broker_frozen=False, broker_freeze_reason=None)
    assert await is_frozen() is False
    await assert_not_frozen()
    policy_snapshot.apply_local(broker_frozen=True, broker_freeze_reason="audit")
    assert await is_frozen() is True
    with pytest.raises(BrokerFrozen):
        await assert_not_frozen()


@pytest.mark.asyncio
async def test_opportunity_policy_dirty_refresh_flow(hp):
    from db import db
    from shared.opportunity.policy import (
        DEFAULTS, get_opportunity_policy, invalidate_policy_cache,
    )
    saved = await db["runtime_flags"].find_one({"_id": "opportunity_policy"})
    try:
        await db["runtime_flags"].delete_one({"_id": "opportunity_policy"})
        invalidate_policy_cache()
        p = await get_opportunity_policy()
        assert p["tiers"]["equity"] == DEFAULTS["tiers"]["equity"]
        await db["runtime_flags"].update_one(
            {"_id": "opportunity_policy"},
            {"$set": {"tiers.crypto.enter": 0.39}}, upsert=True)
        invalidate_policy_cache()
        p2 = await get_opportunity_policy()
        assert p2["tiers"]["crypto"]["enter"] == 0.39
    finally:
        if saved is None:
            await db["runtime_flags"].delete_one({"_id": "opportunity_policy"})
        else:
            await db["runtime_flags"].replace_one(
                {"_id": "opportunity_policy"}, saved, upsert=True)
        invalidate_policy_cache()
