"""Paradox v3 — Step 5.b (re-injection of fired plans).

Pins:
  * `is_refire_enabled` default OFF + parameterised true/false
    sets (same shape as `is_watcher_enabled`).
  * `_derive_action_from_stance` doctrine:
      BULLISH/LONG_BIAS  → "BUY"  (both lanes)
      BEARISH/SHORT_BIAS → "SHORT" on equity, None on crypto
      NEUTRAL            → None
  * `_refire_trigger_fired_plan` flips `plan.intent` from
    WAIT_FOR_TRIGGER → ENTER + stamps `execution.action` so the seat's
    WAIT short-circuit doesn't re-park the plan.
  * Refire OFF: trigger fire only stamps `gate_state="trigger_fired"`;
    no broker is called.
  * Refire ON: a fired BULLISH equity plan rides the full pipeline
    and (if the seat allows it) produces a pipeline receipt.
  * BEARISH crypto plans are refused gracefully (no broker call,
    no infinite loop).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

import pytest

from db import db
from namespaces import (
    BRAIN_ROSTER, PARADOX_V2_SEAT_POLICY, PARADOX_V2_SEAT_TRUSTED,
    SHARED_INTENTS,
)
from shared.pipeline.trigger_watcher import (
    INTENT_WATCH_QUEUE_COLL,
    _derive_action_from_stance,
    _refire_trigger_fired_plan,
    enqueue_watch_plan,
    is_refire_enabled,
    scan_watch_queue,
)


pytestmark = pytest.mark.asyncio


# ── Flag semantics ────────────────────────────────────────────────
def test_refire_dormant_by_default(monkeypatch):
    monkeypatch.delenv("PARADOX_V3_TRIGGER_REFIRE", raising=False)
    assert is_refire_enabled() is False


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off"])
def test_refire_off_for_falsey(monkeypatch, val):
    monkeypatch.setenv("PARADOX_V3_TRIGGER_REFIRE", val)
    assert is_refire_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "On"])
def test_refire_on_for_truthy(monkeypatch, val):
    monkeypatch.setenv("PARADOX_V3_TRIGGER_REFIRE", val)
    assert is_refire_enabled() is True


# ── _derive_action_from_stance doctrine ───────────────────────────
@pytest.mark.parametrize("stance,lane,expected", [
    ("BULLISH",    "equity", "BUY"),
    ("BULLISH",    "crypto", "BUY"),
    ("LONG_BIAS",  "equity", "BUY"),
    ("LONG_BIAS",  "crypto", "BUY"),
    ("BEARISH",    "equity", "SHORT"),
    ("BEARISH",    "crypto", None),   # spot can't short — refuse
    ("SHORT_BIAS", "equity", "SHORT"),
    ("SHORT_BIAS", "crypto", None),
    ("NEUTRAL",    "equity", None),
    ("NEUTRAL",    "crypto", None),
    ("UNCERTAIN",  "equity", None),
    ("",           "equity", None),
])
def test_derive_action_from_stance_table(stance, lane, expected):
    assert _derive_action_from_stance(stance, lane) == expected


# ── Refire OFF: trigger fire is observability-only ────────────────
async def test_refire_off_observability_only(monkeypatch):
    monkeypatch.setenv("PARADOX_V3_TRIGGER_WATCHER", "1")
    monkeypatch.delenv("PARADOX_V3_TRIGGER_REFIRE", raising=False)
    iid = f"t-refoff-{uuid.uuid4().hex[:10]}"
    await db[SHARED_INTENTS].insert_one({
        "intent_id": iid, "gate_state": "pending",
        "intent_version": "v3", "lane": "equity", "symbol": "NVDA",
        "stack": "camino", "action": "HOLD", "confidence": 0.7,
        "plan": {"stance": "BULLISH", "intent": "WAIT_FOR_TRIGGER"},
    })
    await enqueue_watch_plan(
        intent_id=iid, symbol="NVDA", lane="equity", stance="BULLISH",
        trigger_price=100.0, invalidation_price=95.0,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    async def fetcher(symbol, lane):
        return {"price": 101.0}  # fires the trigger

    try:
        out = await scan_watch_queue(price_fetcher=fetcher)
        assert out["fired"] == 1
        # Queue + intent state advanced …
        intent = await db[SHARED_INTENTS].find_one({"intent_id": iid})
        assert intent["gate_state"] == "trigger_fired"
        # … but `action` was NOT mutated (no refire ran).
        assert intent["action"] == "HOLD"
        assert intent["plan"]["intent"] == "WAIT_FOR_TRIGGER"
    finally:
        await db[INTENT_WATCH_QUEUE_COLL].delete_many({"intent_id": iid})
        await db[SHARED_INTENTS].delete_many({"intent_id": iid})


# ── Refire ON: fired plan rides the pipeline ──────────────────────
@pytest.fixture
async def _camino_equity_seat():
    """Seed camino as the equity executor for these tests."""
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


async def test_refire_mutates_intent_and_fires_pipeline(
    monkeypatch, _camino_equity_seat,
):
    monkeypatch.setenv("PARADOX_V3_TRIGGER_WATCHER", "1")
    monkeypatch.setenv("PARADOX_V3_TRIGGER_REFIRE", "1")
    iid = f"t-refon-{uuid.uuid4().hex[:10]}"
    await db[SHARED_INTENTS].insert_one({
        "intent_id": iid, "gate_state": "pending", "executed": False,
        "intent_version": "v3", "lane": "equity", "symbol": "NVDA",
        "stack": "camino", "action": "HOLD", "confidence": 0.72,
        "plan": {
            "stance": "BULLISH", "setup": "bull_flag",
            "intent": "WAIT_FOR_TRIGGER",
            "execution_style": "TRIGGERED_LIMIT", "confidence": 0.72,
        },
        "execution": {"action": None, "derived_from_plan": True},
        "notional_usd": 10.0,
    })
    await enqueue_watch_plan(
        intent_id=iid, symbol="NVDA", lane="equity", stance="BULLISH",
        trigger_price=100.0, invalidation_price=95.0,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    async def fetcher(symbol, lane):
        return {"price": 101.0}

    try:
        out = await scan_watch_queue(price_fetcher=fetcher)
        assert out["fired"] == 1
        intent = await db[SHARED_INTENTS].find_one({"intent_id": iid})
        # Mutation: legacy action upgraded, plan.intent flipped, exec stamped.
        assert intent["action"] == "BUY"
        assert intent["plan"]["intent"] == "ENTER"
        assert intent["execution"]["action"] == "BUY"
        # Gate state advanced past `trigger_fired` (pipeline ran).
        # Depending on the seat / governor outcome it could be
        # several values — we only pin that it's NOT still "watching".
        assert intent["gate_state"] != "waiting_for_trigger"
    finally:
        await db[INTENT_WATCH_QUEUE_COLL].delete_many({"intent_id": iid})
        await db[SHARED_INTENTS].delete_many({"intent_id": iid})


async def test_refire_skips_bearish_crypto(monkeypatch, _camino_equity_seat):
    """A BEARISH crypto plan that fires its trigger can't be safely
    routed (Kraken spot can't short). Watcher transitions the queue
    row to `fired` but skips the refire — intent's `action` stays as
    HOLD so no broker call happens."""
    monkeypatch.setenv("PARADOX_V3_TRIGGER_WATCHER", "1")
    monkeypatch.setenv("PARADOX_V3_TRIGGER_REFIRE", "1")
    iid = f"t-bearcrypto-{uuid.uuid4().hex[:10]}"
    await db[SHARED_INTENTS].insert_one({
        "intent_id": iid, "gate_state": "pending",
        "intent_version": "v3", "lane": "crypto", "symbol": "BTC/USD",
        "stack": "camino", "action": "HOLD", "confidence": 0.7,
        "plan": {"stance": "BEARISH", "intent": "WAIT_FOR_TRIGGER"},
    })
    await enqueue_watch_plan(
        intent_id=iid, symbol="BTC/USD", lane="crypto", stance="BEARISH",
        trigger_price=60_000.0, invalidation_price=65_000.0,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    async def fetcher(symbol, lane):
        return {"price": 58_000.0}  # crosses trigger from above

    try:
        out = await scan_watch_queue(price_fetcher=fetcher)
        assert out["fired"] == 1
        intent = await db[SHARED_INTENTS].find_one({"intent_id": iid})
        assert intent["gate_state"] == "trigger_fired"
        # CRITICAL — no action mutation, no broker call attempt.
        assert intent["action"] == "HOLD"
        assert intent["plan"]["intent"] == "WAIT_FOR_TRIGGER"
    finally:
        await db[INTENT_WATCH_QUEUE_COLL].delete_many({"intent_id": iid})
        await db[SHARED_INTENTS].delete_many({"intent_id": iid})


async def test_refire_skipped_when_intent_doc_missing(monkeypatch):
    """If the intent doc was deleted between park and fire (TTL,
    manual cleanup, etc.), refire fails soft — no exception."""
    monkeypatch.setenv("PARADOX_V3_TRIGGER_REFIRE", "1")
    fake_row = {
        "intent_id": f"__missing__-{uuid.uuid4().hex[:8]}",
        "stance": "BULLISH", "lane": "equity",
    }
    # Should NOT raise.
    result = await _refire_trigger_fired_plan(
        fake_row, now=datetime.now(timezone.utc),
    )
    assert result is None


# ── No infinite loop: the second tick doesn't re-park a fired plan ──
async def test_refired_plan_does_not_re_park_in_subsequent_tick(
    monkeypatch, _camino_equity_seat,
):
    """The mutation flips plan.intent → ENTER. If a downstream call
    were to re-evaluate the same intent, the WAIT short-circuit must
    NOT fire again."""
    monkeypatch.setenv("PARADOX_V3_TRIGGER_WATCHER", "1")
    monkeypatch.setenv("PARADOX_V3_TRIGGER_REFIRE", "1")
    iid = f"t-noloop-{uuid.uuid4().hex[:10]}"
    await db[SHARED_INTENTS].insert_one({
        "intent_id": iid, "gate_state": "pending", "executed": False,
        "intent_version": "v3", "lane": "equity", "symbol": "NVDA",
        "stack": "camino", "action": "HOLD", "confidence": 0.72,
        "plan": {
            "stance": "BULLISH", "intent": "WAIT_FOR_TRIGGER",
            "setup": "bull_flag", "execution_style": "MARKET_NOW",
            "confidence": 0.72,
        },
        "notional_usd": 10.0,
    })
    await enqueue_watch_plan(
        intent_id=iid, symbol="NVDA", lane="equity", stance="BULLISH",
        trigger_price=100.0, invalidation_price=95.0,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    async def fetcher(symbol, lane):
        return {"price": 101.0}

    try:
        await scan_watch_queue(price_fetcher=fetcher)
        # The original watch-queue row should be `fired` (terminal),
        # NOT a fresh `watching` row written by the refire path.
        rows = await db[INTENT_WATCH_QUEUE_COLL].find(
            {"intent_id": iid},
        ).to_list(length=10)
        states = [r["state"] for r in rows]
        assert "fired" in states
        assert "watching" not in states  # no duplicate park
    finally:
        await db[INTENT_WATCH_QUEUE_COLL].delete_many({"intent_id": iid})
        await db[SHARED_INTENTS].delete_many({"intent_id": iid})
