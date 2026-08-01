"""Entry Re-arm Watcher tests (2026-08-01).

"Price falling ≠ entry. Price stabilizing ≠ entry. Price
reaccelerating after support holds = entry candidate." Plus: only
timing blocks re-arm; the child intent carries full lineage and a
NEW confirmation reference — never the stale $8.42.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.risk_sizer.entry_rearm import (  # noqa: E402
    DEFAULT_REARM, REARMABLE_REASONS, detect_pullback_reentry,
)

pytestmark = pytest.mark.tripwire


def _bar(o, c, v=100_000, m=0):
    return {"o": o, "c": c, "h": max(o, c) * 1.002,
            "l": min(o, c) * 0.998, "v": v,
            "ts": f"2026-08-01T14:{m:02d}:00+00:00"}


def _tape(prices, vols=None):
    return [_bar(prices[i - 1] if i else p, p,
                 v=(vols[i] if vols else 100_000), m=i)
            for i, p in enumerate(prices)]


PEAK = 12.47
INVALIDATION = 8.42 * 0.5


def test_price_falling_is_not_an_entry():
    # straight decline off the peak — still falling
    bars = _tape([12.4, 12.1, 11.8, 11.5, 11.2, 11.0, 10.9, 10.8, 10.7, 10.6])
    verdict, r = detect_pullback_reentry(bars, PEAK, INVALIDATION, DEFAULT_REARM)
    assert verdict == "wait", r


def test_price_stabilizing_is_not_an_entry():
    # base at ~10.7 but no reacceleration bar yet
    bars = _tape([12.4, 12.0, 11.5, 11.0, 10.8, 10.72, 10.7, 10.71,
                  10.7, 10.72, 10.71],
                 vols=[300_000, 280_000, 260_000, 240_000, 150_000,
                       120_000, 100_000, 90_000, 85_000, 80_000, 80_000])
    verdict, r = detect_pullback_reentry(bars, PEAK, INVALIDATION, DEFAULT_REARM)
    assert verdict == "wait"
    assert r["why"] in ("stabilizing_not_reaccelerating", "support_lost")


def test_reacceleration_after_base_is_entry_candidate():
    # JDZG continuation: peak 12.47 → base ~10.70 on contracting
    # volume → green reacceleration bar at 10.92 with volume pickup
    bars = _tape([11.4, 11.15, 11.0, 10.9, 10.80, 10.74, 10.72, 10.70,
                  10.71, 10.73, 10.92],
                 vols=[300_000, 280_000, 260_000, 240_000, 150_000,
                       120_000, 100_000, 90_000, 85_000, 80_000, 140_000])
    verdict, r = detect_pullback_reentry(bars, PEAK, INVALIDATION, DEFAULT_REARM)
    assert verdict == "reenter", r
    # new confirmation is the reacceleration price — NOT the old 8.42
    assert abs(r["new_confirmation_price"] - 10.92) < 1e-6
    assert r["new_invalidation_price"] < 10.92


def test_structure_breakdown_invalidates():
    bars = _tape([12.0, 11.0, 9.5, 8.0, 6.5, 5.5, 5.0, 4.5, 4.3, 4.2])
    verdict, r = detect_pullback_reentry(bars, PEAK, INVALIDATION, DEFAULT_REARM)
    assert verdict == "invalidated"


def test_rearmable_reasons_scope():
    assert "MISSED_ENTRY_CHASE_RISK" in REARMABLE_REASONS
    assert "PARABOLIC_CHASE_RISK" in REARMABLE_REASONS
    assert "LATE_MOMENTUM_ENTRY" in REARMABLE_REASONS
    # risk / data / allowlist rejections must NEVER re-arm
    for never in ("NO_TIMING_DATA", "not_in_buy_allowlist",
                  "RISK_LIMIT", "BROKER_REJECTED", "post_sell_cooldown"):
        assert never not in REARMABLE_REASONS


@pytest.mark.asyncio
async def test_non_buy_and_non_timing_blocks_never_create_triggers(monkeypatch):
    from shared.risk_sizer import entry_rearm as mod
    inserted = []

    class _FakeColl:
        async def find_one(self, *a, **k):
            return None
        async def insert_one(self, doc):
            inserted.append(doc)

    class _FakeDB(dict):
        def __getitem__(self, k):
            return _FakeColl()

    import db as dbmod
    monkeypatch.setattr(dbmod, "db", _FakeDB(), raising=False)

    async def _cfg():
        return {**DEFAULT_REARM, "gate_enabled": True}
    monkeypatch.setattr(mod, "get_rearm_config", _cfg)

    await mod.create_trigger({"action": "SELL", "symbol": "X", "intent_id": "i"},
                             "MISSED_ENTRY_CHASE_RISK", {})
    await mod.create_trigger({"action": "BUY", "symbol": "X", "intent_id": "i"},
                             "NO_TIMING_DATA", {})
    assert inserted == []
    await mod.create_trigger(
        {"action": "BUY", "symbol": "X", "lane": "equity", "intent_id": "i"},
        "MISSED_ENTRY_CHASE_RISK", {"current_price": 12.47,
                                    "confirmation_price": 8.42})
    assert len(inserted) == 1
    t = inserted[0]
    assert t["state"] == "WATCHING"
    assert t["original_intent_id"] == "i"
    assert t["block_price"] == 12.47


def test_watcher_wired_into_lifespan():
    src = open("/app/backend/server_modules/lifespan.py").read()
    assert "entry_rearm" in src and "watcher_loop" in src


@pytest.mark.asyncio
async def test_child_intent_is_enqueued_locally(monkeypatch):
    """2026-08-01 validation finding: the router picks from the LOCAL
    intent queue — a Mongo-only child insert is never routed."""
    from shared.risk_sizer import entry_rearm as mod

    inserted, enqueued = [], []

    class _Intents:
        async def find_one(self, *a, **k):
            return {"intent_id": "orig-1", "action": "BUY",
                    "symbol": "X/USD", "lane": "crypto",
                    "snapshot": {"bid": 1.0, "ask": 1.02},
                    "gate_state": "blocked", "executed": False,
                    "route_timeouts": 3, "risk_reason": "old"}
        async def insert_one(self, doc):
            inserted.append(doc)

    class _FakeDB(dict):
        def __getitem__(self, k):
            return _Intents()

    import db as dbmod
    monkeypatch.setattr(dbmod, "db", _FakeDB(), raising=False)
    from shared.hotpath import intent_queue
    monkeypatch.setattr(intent_queue, "enqueue_safe",
                        lambda doc: enqueued.append(doc))

    trigger = {"trigger_id": "trig-1", "original_intent_id": "orig-1"}
    receipt = {"new_confirmation_price": 1.05,
               "new_invalidation_price": 0.98}
    child_id = await mod._emit_child_intent(trigger, receipt)

    assert child_id
    assert len(inserted) == 1 and len(enqueued) == 1
    child = inserted[0]
    assert child["intent_id"] == child_id != "orig-1"
    assert child["gate_state"] == "pending" and child["executed"] is False
    assert child["rearm_of"] == "orig-1"
    assert child["trigger_id"] == "trig-1"
    assert child["snapshot"]["price"] == 1.05
    assert child["stop_price"] == 0.98
    # stale routing state must not ride along
    for k in ("route_timeouts", "risk_reason"):
        assert k not in child
    assert enqueued[0]["intent_id"] == child_id
