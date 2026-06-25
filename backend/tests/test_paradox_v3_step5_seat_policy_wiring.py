"""Paradox v3 — Step 5 wiring tests.

Pins:
  * SeatPolicy parks v3 WAIT_FOR_TRIGGER plans on intent_watch_queue
    and returns a BLOCK verdict (NOT ALLOW) so the broker is never
    called for a parked plan.
  * SeatPolicy still rejects WAIT plans from non-current-seat-holder
    brains (the parking short-circuit runs AFTER the auth checks).
  * `default_price_fetcher` returns sensible shape on
    enrich_snapshot_spread success / failure.
  * Admin endpoints `/api/admin/paradox-v3/status` +
    `/api/admin/paradox-v3/watch-queue` return the expected shape.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

import pytest

from db import db
from namespaces import (
    BRAIN_ROSTER,
    PARADOX_V2_SEAT_POLICY,
    PARADOX_V2_SEAT_TRUSTED,
    SHARED_INTENTS,
)
from shared.pipeline.models import BrainOpinion
from shared.pipeline.seat_policy import SeatPolicy
from shared.pipeline.trigger_watcher import (
    INTENT_WATCH_QUEUE_COLL,
    default_price_fetcher,
)


pytestmark = pytest.mark.asyncio


# ── Fixture: stub a seat + roster + trust row so SeatPolicy passes ──
@pytest.fixture
async def _camino_equity_seat():
    """Set up camino as the current equity executor."""
    await db[PARADOX_V2_SEAT_POLICY].update_one(
        {"seat_id": "equity_executor"},
        {"$set": {
            "seat_id": "equity_executor", "enabled": True,
            "autonomy_mode": "auto_execute",
            "confidence_min": 0.5, "max_notional_usd": 25.0,
        }},
        upsert=True,
    )
    await db[BRAIN_ROSTER].update_one(
        {"_id": "current"},
        {"$set": {"assignments": {"executor": "camino"}}},
        upsert=True,
    )
    await db[PARADOX_V2_SEAT_TRUSTED].update_one(
        {"seat_id": "equity_executor", "brain_id": "camino"},
        {"$set": {"seat_id": "equity_executor", "brain_id": "camino"}},
        upsert=True,
    )
    yield
    # Don't tear down the seat config — other suites depend on it.


def _wait_plan_opinion(brain_id="camino", trigger=187.40, inv=184.20,
                      ttl_seconds=3_600, intent_name="WAIT_FOR_TRIGGER"):
    iid = f"t-v3-{uuid.uuid4().hex[:10]}"
    return BrainOpinion(
        intent_id=iid,
        brain_id=brain_id,
        lane="equity",
        symbol="NVDA",
        action="HOLD",      # legacy field — v3 HOLD lifts to WATCH
        confidence=0.81,
        notional_usd=10.0,
        evidence={},
        intent_version="v3",
        plan={
            "stance": "BULLISH",
            "setup": "bull_flag",
            "intent": intent_name,
            "execution_style": "TRIGGERED_LIMIT",
            "size_posture": "STANDARD",
            "portfolio_posture": "NEUTRAL",
            "confidence": 0.81,
            "trigger_price": trigger,
            "invalidation_price": inv,
            "horizon": "INTRADAY",
            "ttl_seconds": ttl_seconds,
        },
    )


# ── SeatPolicy WAIT-plan parking ──────────────────────────────────
async def test_wait_plan_is_parked_not_executed(_camino_equity_seat):
    """The cornerstone Step 5 contract: a WAIT_FOR_TRIGGER plan from
    the current seat holder lands on the watch queue with a BLOCK
    verdict (NOT ALLOW). The broker is never called."""
    opinion = _wait_plan_opinion()
    # Seed an intent doc so the gate_state update target exists.
    await db[SHARED_INTENTS].insert_one(
        {"intent_id": opinion.intent_id, "gate_state": "pending"}
    )
    try:
        verdict = await SeatPolicy().evaluate(opinion)
        assert verdict.decision == "BLOCK"
        assert "paradox_v3_wait_for_trigger" in verdict.reason
        assert verdict.notional_usd == 0.0
        # Queue row written.
        q = await db[INTENT_WATCH_QUEUE_COLL].find_one(
            {"intent_id": opinion.intent_id}
        )
        assert q is not None
        assert q["state"] == "watching"
        assert q["stance"] == "BULLISH"
        assert q["trigger_price"] == 187.40
        # Intent gate_state stamped.
        intent = await db[SHARED_INTENTS].find_one(
            {"intent_id": opinion.intent_id}
        )
        assert intent["gate_state"] == "waiting_for_trigger"
    finally:
        await db[INTENT_WATCH_QUEUE_COLL].delete_many(
            {"intent_id": opinion.intent_id}
        )
        await db[SHARED_INTENTS].delete_many(
            {"intent_id": opinion.intent_id}
        )


async def test_wait_confirmation_also_parks(_camino_equity_seat):
    """Sibling enum value `WAIT_CONFIRMATION` also routes to the
    queue (per operator §11 — both wait forms discipline-scored)."""
    opinion = _wait_plan_opinion(intent_name="WAIT_CONFIRMATION")
    await db[SHARED_INTENTS].insert_one(
        {"intent_id": opinion.intent_id, "gate_state": "pending"}
    )
    try:
        verdict = await SeatPolicy().evaluate(opinion)
        assert verdict.decision == "BLOCK"
        assert "paradox_v3_wait_confirmation" in verdict.reason
        q = await db[INTENT_WATCH_QUEUE_COLL].find_one(
            {"intent_id": opinion.intent_id}
        )
        assert q is not None
    finally:
        await db[INTENT_WATCH_QUEUE_COLL].delete_many(
            {"intent_id": opinion.intent_id}
        )
        await db[SHARED_INTENTS].delete_many(
            {"intent_id": opinion.intent_id}
        )


async def test_non_executor_wait_plan_still_blocked_for_auth(_camino_equity_seat):
    """Auth runs BEFORE the WAIT short-circuit. A non-executor brain
    posting a WAIT plan still gets `brain_not_current_seat_holder`
    so the queue stays clean."""
    opinion = _wait_plan_opinion(brain_id="barracuda")
    await db[SHARED_INTENTS].insert_one(
        {"intent_id": opinion.intent_id, "gate_state": "pending"}
    )
    try:
        verdict = await SeatPolicy().evaluate(opinion)
        assert verdict.decision == "BLOCK"
        assert "brain_not_current_seat_holder" in verdict.reason
        # CRITICAL — NO queue row written for a non-executor WAIT plan.
        q = await db[INTENT_WATCH_QUEUE_COLL].find_one(
            {"intent_id": opinion.intent_id}
        )
        assert q is None
    finally:
        await db[SHARED_INTENTS].delete_many(
            {"intent_id": opinion.intent_id}
        )


async def test_v3_enter_plan_proceeds_through_normal_seat_path(_camino_equity_seat):
    """A v3 plan with intent=ENTER (not WAIT_*) must NOT short-circuit
    — the normal confidence/consensus path runs as on v2."""
    iid = f"t-enter-{uuid.uuid4().hex[:10]}"
    opinion = BrainOpinion(
        intent_id=iid, brain_id="camino", lane="equity", symbol="AAPL",
        action="BUY", confidence=0.72, notional_usd=10.0,
        intent_version="v3",
        plan={
            "stance": "BULLISH", "setup": "breakout", "intent": "ENTER",
            "execution_style": "MARKET_NOW", "confidence": 0.72,
        },
    )
    await db[SHARED_INTENTS].insert_one(
        {"intent_id": iid, "gate_state": "pending"}
    )
    try:
        verdict = await SeatPolicy().evaluate(opinion)
        # ENTER plan with conf=0.72 >= conf_min=0.5 → ALLOW.
        assert verdict.decision == "ALLOW"
        # No queue row.
        q = await db[INTENT_WATCH_QUEUE_COLL].find_one({"intent_id": iid})
        assert q is None
    finally:
        await db[SHARED_INTENTS].delete_many({"intent_id": iid})


async def test_v2_intent_does_not_park(_camino_equity_seat):
    """Legacy v2 intents (no plan field) take the normal path —
    nothing about Step 5 changes their behaviour."""
    iid = f"t-v2-{uuid.uuid4().hex[:10]}"
    opinion = BrainOpinion(
        intent_id=iid, brain_id="camino", lane="equity", symbol="AAPL",
        action="BUY", confidence=0.7, notional_usd=10.0,
        # intent_version + plan unset — the default v2 path.
    )
    await db[SHARED_INTENTS].insert_one(
        {"intent_id": iid, "gate_state": "pending"}
    )
    try:
        verdict = await SeatPolicy().evaluate(opinion)
        assert verdict.decision == "ALLOW"
        q = await db[INTENT_WATCH_QUEUE_COLL].find_one({"intent_id": iid})
        assert q is None
    finally:
        await db[SHARED_INTENTS].delete_many({"intent_id": iid})


# ── default_price_fetcher fail-soft behaviour ─────────────────────
async def test_default_price_fetcher_returns_none_on_unknown_symbol(monkeypatch):
    """Unknown symbols / no quote → None (NOT exception)."""
    # Force the enricher to return an empty snapshot (no quotes).
    async def stub_enrich(snap, *, symbol, lane):
        return ({}, {"attempts": []})

    monkeypatch.setattr(
        "shared.market_data.enrich_snapshot_spread",
        stub_enrich,
    )
    out = await default_price_fetcher("__GARBAGE__", "equity")
    assert out is None


async def test_default_price_fetcher_returns_mid_when_bid_ask_present(monkeypatch):
    async def stub_enrich(snap, *, symbol, lane):
        return ({"bid": 100.0, "ask": 100.10}, {"attempts": []})

    monkeypatch.setattr(
        "shared.market_data.enrich_snapshot_spread",
        stub_enrich,
    )
    out = await default_price_fetcher("ANY", "equity")
    assert out is not None
    assert abs(out["price"] - 100.05) < 1e-6


async def test_default_price_fetcher_swallows_exception(monkeypatch):
    async def stub_enrich(snap, *, symbol, lane):
        raise RuntimeError("kraken down")

    monkeypatch.setattr(
        "shared.market_data.enrich_snapshot_spread",
        stub_enrich,
    )
    out = await default_price_fetcher("ANY", "equity")
    assert out is None  # No exception bubbles up.


# ── Admin endpoints ───────────────────────────────────────────────
async def test_status_endpoint_reports_dormant_when_flags_unset(monkeypatch):
    monkeypatch.delenv("PARADOX_V3_BRAINS", raising=False)
    monkeypatch.delenv("PARADOX_V3_TRIGGER_WATCHER", raising=False)
    from routes.admin_paradox_v3 import paradox_v3_status
    out = await paradox_v3_status(_user={"email": "admin"})
    assert out["brains_on_v3"] == []
    assert out["trigger_watcher_enabled"] is False
    assert out["rollout_step"] == "steps_1_to_3_rails_only"


async def test_status_endpoint_reports_shadow_when_only_brain_flag_set(monkeypatch):
    monkeypatch.setenv("PARADOX_V3_BRAINS", "camino")
    monkeypatch.delenv("PARADOX_V3_TRIGGER_WATCHER", raising=False)
    from routes.admin_paradox_v3 import paradox_v3_status
    out = await paradox_v3_status(_user={"email": "admin"})
    assert out["brains_on_v3"] == ["camino"]
    assert out["trigger_watcher_enabled"] is False
    assert out["rollout_step"] == "step_4_shadow_emit_only"


async def test_status_endpoint_reports_live_when_both_flags_set(monkeypatch):
    monkeypatch.setenv("PARADOX_V3_BRAINS", "camino,barracuda")
    monkeypatch.setenv("PARADOX_V3_TRIGGER_WATCHER", "1")
    from routes.admin_paradox_v3 import paradox_v3_status
    out = await paradox_v3_status(_user={"email": "admin"})
    assert sorted(out["brains_on_v3"]) == ["barracuda", "camino"]
    assert out["trigger_watcher_enabled"] is True
    assert out["rollout_step"] == "step_5_trigger_watcher_live"


async def test_watch_queue_endpoint_returns_snapshot_shape():
    from routes.admin_paradox_v3 import paradox_v3_watch_queue
    out = await paradox_v3_watch_queue(limit=10, _user={"email": "admin"})
    assert "enabled" in out
    assert "counts" in out
    for state in ("watching", "fired", "invalidated", "expired"):
        assert state in out["counts"]
    assert isinstance(out["recent"], list)


# ── End-to-end: full unified pipeline parks a v3 WAIT plan ─────────
async def test_full_pipeline_parks_v3_wait_intent_with_legacy_hold_action(_camino_equity_seat):
    """The critical regression: a v3 WAIT_FOR_TRIGGER intent carries
    `action=HOLD` per §6.2 mapping. The legacy execution_pipeline
    HOLD short-circuit MUST be bypassed for v3 WAIT plans — otherwise
    the seat never sees them and the plan dies at the brain layer
    with `final_reason=brain_hold`."""
    from shared.pipeline.adapter import run_unified_for_intent
    iid = f"t-e2e-{uuid.uuid4().hex[:10]}"
    intent = {
        "intent_id": iid, "stack": "camino", "symbol": "NVDA",
        "lane": "equity", "action": "HOLD", "confidence": 0.81,
        "intent_version": "v3",
        "plan": {
            "stance": "BULLISH", "setup": "bull_flag",
            "intent": "WAIT_FOR_TRIGGER", "execution_style": "TRIGGERED_LIMIT",
            "confidence": 0.81, "trigger_price": 100.0,
            "invalidation_price": 95.0, "horizon": "INTRADAY",
            "ttl_seconds": 3600,
        },
    }
    await db[SHARED_INTENTS].insert_one(
        dict(intent, gate_state="pending", executed=False),
    )
    try:
        verdict = await run_unified_for_intent(intent, 10.0)
        # Must NOT be a brain-side HOLD short-circuit.
        assert "brain_hold" not in verdict["reason"]
        assert "paradox_v3_wait_for_trigger" in verdict["reason"]
        assert verdict["verdict"] == "no_trade"
        # Queue + intent state both flipped.
        q = await db[INTENT_WATCH_QUEUE_COLL].find_one({"intent_id": iid})
        assert q is not None
        assert q["state"] == "watching"
        intent_doc = await db[SHARED_INTENTS].find_one({"intent_id": iid})
        assert intent_doc["gate_state"] == "waiting_for_trigger"
    finally:
        await db[INTENT_WATCH_QUEUE_COLL].delete_many({"intent_id": iid})
        await db[SHARED_INTENTS].delete_many({"intent_id": iid})


async def test_v2_hold_intent_still_short_circuits_to_no_order(_camino_equity_seat):
    """The HOLD-bypass MUST be narrow — only v3 WAIT_* plans skip the
    short-circuit. A legacy v2 HOLD intent still receives the
    `brain_hold` NO_ORDER receipt as before."""
    from shared.pipeline.adapter import run_unified_for_intent
    iid = f"t-v2-hold-{uuid.uuid4().hex[:10]}"
    intent = {
        "intent_id": iid, "stack": "camino", "symbol": "NVDA",
        "lane": "equity", "action": "HOLD", "confidence": 0.5,
        # No intent_version, no plan — legacy v2 HOLD.
    }
    await db[SHARED_INTENTS].insert_one(
        dict(intent, gate_state="pending", executed=False),
    )
    try:
        verdict = await run_unified_for_intent(intent, 10.0)
        assert "brain_hold" in verdict["reason"]
        q = await db[INTENT_WATCH_QUEUE_COLL].find_one({"intent_id": iid})
        assert q is None  # Never reached the seat.
    finally:
        await db[SHARED_INTENTS].delete_many({"intent_id": iid})


# ── Crypto-lane parity (operator pin 2026-02-22) ──────────────────
@pytest.fixture
async def _camino_crypto_seat():
    """Set up camino as the current crypto executor + crypto_executor
    seat config + trust. Cleaned up after the test so the preview
    deployment's vacant-crypto-seat posture is restored."""
    await db[PARADOX_V2_SEAT_POLICY].update_one(
        {"seat_id": "crypto_executor"},
        {"$set": {
            "seat_id": "crypto_executor", "enabled": True,
            "autonomy_mode": "auto_execute",
            "confidence_min": 0.5, "max_notional_usd": 25.0,
        }},
        upsert=True,
    )
    roster_before = await db[BRAIN_ROSTER].find_one(
        {"_id": "current"}, {"assignments": 1, "_id": 0},
    )
    prev_assignments = (roster_before or {}).get("assignments") or {}
    new_assignments = dict(prev_assignments)
    new_assignments["crypto"] = "camino"
    await db[BRAIN_ROSTER].update_one(
        {"_id": "current"},
        {"$set": {"assignments": new_assignments}},
        upsert=True,
    )
    await db[PARADOX_V2_SEAT_TRUSTED].update_one(
        {"seat_id": "crypto_executor", "brain_id": "camino"},
        {"$set": {"seat_id": "crypto_executor", "brain_id": "camino"}},
        upsert=True,
    )
    yield
    # Restore the prior roster posture (preview has crypto vacant).
    await db[BRAIN_ROSTER].update_one(
        {"_id": "current"},
        {"$set": {"assignments": prev_assignments}},
        upsert=True,
    )


async def test_crypto_wait_plan_is_parked(_camino_crypto_seat):
    """The same WAIT_FOR_TRIGGER short-circuit must work on the crypto
    lane. SeatPolicy reads the lane → executor_seat map and routes
    through the crypto roster slot."""
    iid = f"t-crypto-wait-{uuid.uuid4().hex[:10]}"
    opinion = BrainOpinion(
        intent_id=iid, brain_id="camino", lane="crypto",
        symbol="BTC/USD",
        action="HOLD", confidence=0.78, notional_usd=10.0,
        intent_version="v3",
        plan={
            "stance": "BULLISH", "setup": "breakout",
            "intent": "WAIT_FOR_TRIGGER",
            "execution_style": "TRIGGERED_LIMIT",
            "confidence": 0.78,
            "trigger_price": 65_000.0,
            "invalidation_price": 62_000.0,
            "horizon": "SWING",
            "ttl_seconds": 86_400,
        },
    )
    await db[SHARED_INTENTS].insert_one(
        {"intent_id": iid, "gate_state": "pending"}
    )
    try:
        verdict = await SeatPolicy().evaluate(opinion)
        assert verdict.decision == "BLOCK"
        assert "paradox_v3_wait_for_trigger" in verdict.reason
        q = await db[INTENT_WATCH_QUEUE_COLL].find_one({"intent_id": iid})
        assert q is not None
        assert q["lane"] == "crypto"
        assert q["symbol"] == "BTC/USD"
        assert q["trigger_price"] == 65_000.0
    finally:
        await db[INTENT_WATCH_QUEUE_COLL].delete_many({"intent_id": iid})
        await db[SHARED_INTENTS].delete_many({"intent_id": iid})


async def test_crypto_wait_blocked_when_seat_vacant():
    """If the operator hasn't pinned a crypto executor, even a WAIT
    plan from a trusted brain gets blocked at the seat-vacancy
    auth gate. NO queue row is written.

    This is the preview deployment's CURRENT posture (operator's
    pin 2026-02-22 — `crypto: None` in the roster). Surfacing this
    contract here so a future doctrine change requires updating the
    test deliberately."""
    iid = f"t-crypto-vacant-{uuid.uuid4().hex[:10]}"
    # Ensure crypto seat is vacant for this test.
    roster_before = await db[BRAIN_ROSTER].find_one(
        {"_id": "current"}, {"_id": 0, "assignments": 1},
    ) or {}
    prev = roster_before.get("assignments") or {}
    forced = dict(prev)
    forced["crypto"] = None
    await db[BRAIN_ROSTER].update_one(
        {"_id": "current"},
        {"$set": {"assignments": forced}},
        upsert=True,
    )
    opinion = BrainOpinion(
        intent_id=iid, brain_id="camino", lane="crypto",
        symbol="BTC/USD",
        action="HOLD", confidence=0.78, notional_usd=10.0,
        intent_version="v3",
        plan={
            "stance": "BULLISH", "setup": "breakout",
            "intent": "WAIT_FOR_TRIGGER",
            "execution_style": "TRIGGERED_LIMIT",
            "confidence": 0.78, "trigger_price": 65_000.0,
        },
    )
    await db[SHARED_INTENTS].insert_one(
        {"intent_id": iid, "gate_state": "pending"}
    )
    try:
        verdict = await SeatPolicy().evaluate(opinion)
        assert verdict.decision == "BLOCK"
        assert "executor_seat_vacant:crypto_executor" in verdict.reason
        q = await db[INTENT_WATCH_QUEUE_COLL].find_one({"intent_id": iid})
        assert q is None
    finally:
        await db[BRAIN_ROSTER].update_one(
            {"_id": "current"},
            {"$set": {"assignments": prev}},
            upsert=True,
        )
        await db[SHARED_INTENTS].delete_many({"intent_id": iid})


async def test_default_price_fetcher_works_for_crypto_lane(monkeypatch):
    """The default fetcher must handle both lanes — the
    `enrich_snapshot_spread` ladder hits Kraken-public for crypto
    and Webull for equity. Stub the enricher to confirm the lane
    arg is threaded through."""
    captured = {}

    async def stub_enrich(snap, *, symbol, lane):
        captured["symbol"] = symbol
        captured["lane"] = lane
        return ({"bid": 64_995.0, "ask": 65_005.0}, {"attempts": []})

    monkeypatch.setattr(
        "shared.market_data.enrich_snapshot_spread",
        stub_enrich,
    )
    out = await default_price_fetcher("BTC/USD", "crypto")
    assert out == {"price": 65_000.0}
    assert captured == {"symbol": "BTC/USD", "lane": "crypto"}


async def test_status_endpoint_reports_lane_seat_posture():
    """The status endpoint surfaces the per-lane executor posture so
    the operator can see at a glance which lanes are eligible for
    WAIT plan parking."""
    from routes.admin_paradox_v3 import paradox_v3_status
    out = await paradox_v3_status(_user={"email": "admin"})
    assert "lane_executor_seats" in out
    assert "equity" in out["lane_executor_seats"]
    assert "crypto" in out["lane_executor_seats"]
    for lane_state in out["lane_executor_seats"].values():
        assert "executor_holder" in lane_state
        assert "wait_plans_eligible" in lane_state
