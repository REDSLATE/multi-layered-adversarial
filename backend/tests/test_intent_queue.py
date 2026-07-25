"""Local durable intent queue — hot-path audit P2 #6 (2026-07-24).

Pins: enqueue/pick semantics mirror the legacy Atlas router query
(newest-first, lookback, terminal gate_states, poison guard), verdict
mark-back, executed idempotency, SQLite restart recovery, and the
supervisor tick picking from the LOCAL store.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "/app/backend")

from shared.hotpath import daily_spend, intent_queue, outbox, policy_snapshot


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _intent(iid: str, minutes_ago: float = 1.0, **over) -> dict:
    doc = {
        "intent_id": iid,
        "ingest_ts": (_now() - timedelta(minutes=minutes_ago)).isoformat(),
        "lane": "crypto",
        "symbol": "BTC/USD",
        "action": "BUY",
        "confidence": 0.5,
        "executed": False,
    }
    doc.update(over)
    return doc


def test_pick_newest_first_with_limit(hp):
    intent_queue.enqueue(_intent("q-old", minutes_ago=30))
    intent_queue.enqueue(_intent("q-mid", minutes_ago=10))
    intent_queue.enqueue(_intent("q-new", minutes_ago=1))
    picked = intent_queue.pick(limit=2)
    assert [p["intent_id"] for p in picked] == ["q-new", "q-mid"]


def test_pick_filters_mirror_atlas_query(hp):
    intent_queue.enqueue(_intent("q-ok"))
    intent_queue.enqueue(_intent("q-exec", executed=True))
    intent_queue.enqueue(_intent("q-blocked", gate_state="blocked"))
    intent_queue.enqueue(_intent("q-submitted", gate_state="submitted"))
    intent_queue.enqueue(_intent("q-stale", minutes_ago=120))
    intent_queue.enqueue(_intent("q-hold", action="HOLD"))
    intent_queue.enqueue(_intent("q-nosym", symbol=None))
    intent_queue.enqueue(_intent("q-poison", route_timeouts=3))
    picked = intent_queue.pick(limit=10, lookback_min=60)
    assert [p["intent_id"] for p in picked] == ["q-ok"]


def test_mark_from_verdict_and_is_executed(hp):
    intent_queue.enqueue(_intent("q-v1"))
    intent_queue.enqueue(_intent("q-v2"))
    intent_queue.enqueue(_intent("q-v3"))
    intent_queue.mark_from_verdict("q-v1", {"verdict": "executed"})
    intent_queue.mark_from_verdict("q-v2", {"verdict": "blocked"})
    intent_queue.mark_from_verdict("q-v3", {"verdict": "error"})  # stays pending
    picked = intent_queue.pick(limit=10)
    assert [p["intent_id"] for p in picked] == ["q-v3"]
    assert intent_queue.is_executed("q-v1") is True
    assert intent_queue.is_executed("q-v2") is False


def test_route_timeout_poison(hp):
    intent_queue.enqueue(_intent("q-t1"))
    assert intent_queue.bump_route_timeout("q-t1", 3) == 1
    assert intent_queue.bump_route_timeout("q-t1", 3) == 2
    assert [p["intent_id"] for p in intent_queue.pick(limit=10)] == ["q-t1"]
    assert intent_queue.bump_route_timeout("q-t1", 3) == 3
    assert intent_queue.pick(limit=10) == []


def test_sqlite_restart_recovery(hp):
    intent_queue.enqueue(_intent("q-r1"))
    intent_queue.mark("q-r1", executed=True)
    intent_queue.enqueue(_intent("q-r2"))
    # Simulated restart: memory wiped, SQLite reloads.
    intent_queue._cache.clear()  # noqa: SLF001
    intent_queue._cache_loaded = False  # noqa: SLF001
    assert [p["intent_id"] for p in intent_queue.pick(limit=10)] == ["q-r2"]
    assert intent_queue.is_executed("q-r1") is True


@pytest.mark.asyncio
async def test_risk_check_concurrency_uses_local_queue(hp, monkeypatch):
    from shared.risk.check import check
    monkeypatch.setenv("RISEDUAL_CAP_DAILY_USD", "1000")
    policy_snapshot._dirty = False  # noqa: SLF001
    policy_snapshot.apply_local(
        master_switch_enabled=True,
        lane_enabled={"equity": True, "crypto": True},
    )
    intent_queue.enqueue(_intent("q-idem-1"))
    intent_queue.mark("q-idem-1", executed=True)
    r = await check({"intent_id": "q-idem-1", "lane": "crypto"}, notional_usd=5.0)
    assert r.ok is False and r.reason == "already_executed_concurrent"


@pytest.mark.asyncio
async def test_supervisor_tick_picks_from_local_queue(hp, monkeypatch):
    """End-to-end tick: local queue feeds `_route_one`, verdict marks
    back, NO Atlas pick query needed."""
    import shared.auto_router as ar
    import shared.auto_router_supervisor as sup

    intent_queue.enqueue(_intent("q-tick-1"))
    routed: list[str] = []

    async def _fake_route_one(intent):
        routed.append(intent["intent_id"])
        return {"verdict": "executed", "intent_id": intent["intent_id"]}

    async def _noop():
        return None

    async def _armed():
        return True

    monkeypatch.setattr(ar, "_route_one", _fake_route_one)
    monkeypatch.setattr(ar, "_sweep_expired_unrouted", _noop)
    monkeypatch.setattr(ar, "_sweep_submitted_broker_orders", _noop)
    monkeypatch.setattr(ar, "_is_master_switch_armed", _armed)

    results = await sup._tick()
    assert routed == ["q-tick-1"]
    assert results and results[0]["verdict"] == "executed"
    assert sup._LAST_TICK_QUEUE_SOURCE == "local"
    # Verdict marked back — second tick picks nothing.
    routed.clear()
    await sup._tick()
    assert routed == []
    assert intent_queue.is_executed("q-tick-1") is True
