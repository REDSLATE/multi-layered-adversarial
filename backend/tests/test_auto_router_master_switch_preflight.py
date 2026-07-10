"""Master-switch preflight — auto_router must consult the operator's
arm gate before routing any intent.

Prior to 2026-02-19 the master switch was UI-only: `POST /api/admin/trading/toggle`
wrote to Mongo, the status endpoint read it back, but the auto_router
loop never consulted it. Only `AUTO_ROUTER_ENABLED` env at boot
gated the pipeline. This test suite locks in the runtime doctrine:

    * `_tick` short-circuits when master switch is disarmed
      (no intent submissions).
    * `_tick` STILL runs the reconcile sweep even when disarmed
      (in-flight orders must not be stranded).
    * `_route_one` refuses to submit when disarmed (backdoor guard
      for the manual `/api/execution/submit` endpoint).
    * Cached reader hits Mongo once per TTL window then serves from
      cache; `_invalidate_arm_cache()` forces a fresh read.
    * Fail-CLOSED on Mongo read error (disarm on unknown state,
      never accidentally arm).

No paper. No dry_run. All tests use in-process mocks; no broker
network calls.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, "/app/backend")

from db import db  # noqa: E402
from shared import auto_router  # noqa: E402
from namespaces import SHARED_INTENTS  # noqa: E402


_TEST_PREFIX = "test-arm-preflight-"


@pytest.fixture(autouse=True)
async def _purge_intents():
    """Purge synthetic intents before + after each test AND reset
    the auto_router arm cache."""
    await db[SHARED_INTENTS].delete_many(
        {"intent_id": {"$regex": f"^{_TEST_PREFIX}"}},
    )
    auto_router._invalidate_arm_cache()
    yield
    await db[SHARED_INTENTS].delete_many(
        {"intent_id": {"$regex": f"^{_TEST_PREFIX}"}},
    )
    auto_router._invalidate_arm_cache()


async def _seed_intent(intent_id: str, minutes_ago: int = 1) -> None:
    ts = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).isoformat()
    await db[SHARED_INTENTS].insert_one({
        "intent_id": intent_id,
        "stack": "test-brain",
        "stack_canonical": "test-brain",
        "action": "BUY",
        "symbol": "AAPL",
        "lane": "equity",
        "canonical": "EQ:AAPL",
        "confidence": 0.75,
        "ingest_ts": ts,
        "gate_state": "pending",
        "executed": False,
        "execution": {"notional_usd": 5.0},
    })


# ═══════════════════════════════════════════════════════════════════
#  Cached reader
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_master_switch_defaults_to_disarmed_when_no_doc(monkeypatch):
    """No `trading_controls` doc yet → armed=False. Fail-closed."""
    async def _fake_read():
        return False  # simulates first_boot_default_disabled

    monkeypatch.setattr(
        "routes.trading_controls.is_trading_enabled",
        _fake_read,
    )
    auto_router._invalidate_arm_cache()
    assert await auto_router._is_master_switch_armed() is False


@pytest.mark.asyncio
async def test_master_switch_returns_true_when_armed(monkeypatch):
    async def _fake_read():
        return True

    monkeypatch.setattr(
        "routes.trading_controls.is_trading_enabled",
        _fake_read,
    )
    auto_router._invalidate_arm_cache()
    assert await auto_router._is_master_switch_armed() is True


@pytest.mark.asyncio
async def test_master_switch_fails_closed_on_read_error(monkeypatch):
    """A Mongo read failure MUST NOT accidentally arm the loop."""
    async def _boom():
        raise RuntimeError("mongo unreachable")

    monkeypatch.setattr(
        "routes.trading_controls.is_trading_enabled",
        _boom,
    )
    auto_router._invalidate_arm_cache()
    assert await auto_router._is_master_switch_armed() is False


@pytest.mark.asyncio
async def test_arm_cache_hits_within_ttl(monkeypatch):
    """Two consecutive reads within TTL hit Mongo only once."""
    hit_count = 0

    async def _counting_read():
        nonlocal hit_count
        hit_count += 1
        return True

    monkeypatch.setattr(
        "routes.trading_controls.is_trading_enabled",
        _counting_read,
    )
    auto_router._invalidate_arm_cache()
    await auto_router._is_master_switch_armed()
    await auto_router._is_master_switch_armed()
    await auto_router._is_master_switch_armed()
    assert hit_count == 1, "arm cache should not re-read within TTL"


@pytest.mark.asyncio
async def test_invalidate_arm_cache_forces_fresh_read(monkeypatch):
    """`_invalidate_arm_cache()` must force the next call to hit Mongo,
    so operator toggle flips take effect immediately (no TTL wait)."""
    hit_count = 0
    return_val = True

    async def _counting_read():
        nonlocal hit_count
        hit_count += 1
        return return_val

    monkeypatch.setattr(
        "routes.trading_controls.is_trading_enabled",
        _counting_read,
    )
    auto_router._invalidate_arm_cache()
    assert await auto_router._is_master_switch_armed() is True
    # Simulate operator disarm.
    return_val = False
    auto_router._invalidate_arm_cache()
    assert await auto_router._is_master_switch_armed() is False
    assert hit_count == 2, "invalidate must force a re-read"


# ═══════════════════════════════════════════════════════════════════
#  _route_one preflight (manual-submit backdoor guard)
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_route_one_short_circuits_when_disarmed(monkeypatch):
    """Manual /api/execution/submit routes through _route_one — the
    switch MUST gate this backdoor too."""
    async def _disarmed():
        return False

    monkeypatch.setattr(
        "routes.trading_controls.is_trading_enabled",
        _disarmed,
    )
    auto_router._invalidate_arm_cache()

    intent_id = f"{_TEST_PREFIX}route-disarmed"
    await _seed_intent(intent_id)

    result = await auto_router._route_one({
        "intent_id": intent_id, "action": "BUY", "symbol": "AAPL",
        "lane": "equity",
    })
    assert result["verdict"] == "blocked"
    assert result["reason"] == "master_switch_disarmed"

    # The intent doc must carry the block reason for the funnel.
    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc["gate_state"] == "blocked"
    assert doc["broker_reason"] == "master_switch_disarmed"


@pytest.mark.asyncio
async def test_route_one_proceeds_when_armed(monkeypatch):
    """When armed, _route_one MUST progress past the arm gate. We
    don't care what happens downstream (seat/risk/broker) — the
    single invariant is that the intent doc is NOT stamped with
    `master_switch_disarmed` when the switch is armed."""
    async def _armed():
        return True

    monkeypatch.setattr(
        "routes.trading_controls.is_trading_enabled",
        _armed,
    )
    auto_router._invalidate_arm_cache()

    intent_id = f"{_TEST_PREFIX}route-armed"
    await _seed_intent(intent_id)

    # Let the downstream flow do whatever it likes; we only assert
    # the arm gate did not short-circuit.
    try:
        result = await auto_router._route_one({
            "intent_id": intent_id, "action": "BUY", "symbol": "AAPL",
            "lane": "equity",
        })
    except Exception:  # noqa: BLE001
        # Downstream (seat/risk/broker) may raise in the test env —
        # that's fine, it proves the arm gate didn't block.
        result = {"reason": None}
    assert result.get("reason") != "master_switch_disarmed"

    # And the intent doc must NOT carry that specific block reason.
    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert (
        doc.get("broker_reason") != "master_switch_disarmed"
    ), "arm gate short-circuited despite switch being ARMED"


# ═══════════════════════════════════════════════════════════════════
#  _tick preflight — reconcile still runs, ingestion does not
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tick_returns_empty_when_disarmed(monkeypatch):
    """When disarmed, _tick MUST NOT pull new intents. Empty result."""
    async def _disarmed():
        return False

    monkeypatch.setattr(
        "routes.trading_controls.is_trading_enabled",
        _disarmed,
    )
    auto_router._invalidate_arm_cache()

    # Seed a fresh, unrouted intent that WOULD normally be picked up.
    await _seed_intent(f"{_TEST_PREFIX}tick-disarmed-1")

    # Also monkeypatch the two sweeps so we don't hit real broker code.
    async def _noop_sweep():
        return None

    monkeypatch.setattr(
        auto_router, "_sweep_expired_unrouted", _noop_sweep,
    )
    monkeypatch.setattr(
        auto_router, "_sweep_submitted_broker_orders", _noop_sweep,
    )

    results = await auto_router._tick()
    assert results == [], "disarmed tick must not ingest new intents"


@pytest.mark.asyncio
async def test_tick_still_runs_reconcile_sweep_when_disarmed(monkeypatch):
    """A disarm mid-flight MUST NOT strand a submitted order —
    reconcile sweep runs unconditionally, even when disarmed."""
    async def _disarmed():
        return False

    monkeypatch.setattr(
        "routes.trading_controls.is_trading_enabled",
        _disarmed,
    )
    auto_router._invalidate_arm_cache()

    called = {"expired": 0, "reconcile": 0}

    async def _mock_expired():
        called["expired"] += 1

    async def _mock_reconcile():
        called["reconcile"] += 1

    monkeypatch.setattr(auto_router, "_sweep_expired_unrouted", _mock_expired)
    monkeypatch.setattr(
        auto_router, "_sweep_submitted_broker_orders", _mock_reconcile,
    )

    await auto_router._tick()
    assert called["expired"] == 1, (
        "expired-unrouted sweep must still run when disarmed"
    )
    assert called["reconcile"] == 1, (
        "broker-order reconcile sweep must still run when disarmed"
    )
