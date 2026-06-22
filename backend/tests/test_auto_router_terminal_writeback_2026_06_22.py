"""Regression: auto-router must terminally mark non-executed pipeline
verdicts on `shared_intents.gate_state` so the next tick stops re-
processing the same intent.

Why this exists (2026-06-22 P0 fix — Seat-Drift / Funnel-Leak):

Production funnel showed 6,459 of 6,464 intents (100% leak) dropped
between EMITTED → SEAT_APPROVED. Investigation revealed the Unified
Pipeline writes its own receipt to `pipeline_receipts` but never
updates the canonical `shared_intents.gate_state`. Without writeback,
5 stuck TRIPWIRE intents (emitted by `camaro` while `barracuda` held
the equity executor seat) looped through the 30s auto-router tick at
5/tick — forever — appearing as 6,459 seat blocks in the funnel.

The fix: after `_route_one` returns a non-executed verdict
(no_trade/blocked/advisory_only), stamp the terminal state onto the
intent so the next tick skips it via the existing
`gate_state $nin [blocked, no_trade, advisory_only]` filter.

This test pins the contract:
  * BLOCKED   → gate_state = "blocked"
  * NO_TRADE  → gate_state = "blocked"  (same terminal bucket)
  * ADVISORY  → gate_state = "advisory_only"
  * EXECUTED  → no writeback (already handled by execution_submit)
  * ERROR     → no writeback (broker may recover, allow retry)
"""
from __future__ import annotations

import sys
import pytest

sys.path.insert(0, "/app/backend")


@pytest.mark.asyncio
async def test_blocked_verdict_marks_gate_state_blocked(monkeypatch):
    """A `verdict=no_trade` from the Unified Pipeline must flip the
    canonical gate_state to `blocked` so the same intent can't be
    re-evaluated on the next 30s tick."""
    from shared import auto_router

    updates: list[tuple[dict, dict]] = []

    class FakeColl:
        async def update_one(self, q, u):
            updates.append((q, u))

        def find(self, *args, **kwargs):
            class _C:
                def sort(self_, *a, **k): return self_
                async def to_list(self_, *a, **k):
                    return [{
                        "intent_id": "I1",
                        "lane": "equity",
                        "stack": "camaro",
                        "symbol": "TRIPWIRE_SPREAD_A",
                        "action": "BUY",
                    }]
            return _C()

    class FakeDB:
        def __getitem__(self, name):
            return FakeColl()

    monkeypatch.setattr(auto_router, "db", FakeDB())

    async def fake_route_one(intent):
        return {
            "intent_id": intent.get("intent_id"),
            "verdict": "no_trade",
            "reason": "brain_not_current_seat_holder:camaro!=barracuda@equity_executor",
            "final_status": "BLOCKED",
        }

    monkeypatch.setattr(auto_router, "_route_one", fake_route_one)
    monkeypatch.setattr(auto_router, "_sweep_seat_mismatched_intents",
                        lambda: _async_zero())

    async def fake_lane_eligible(lane: str) -> bool:
        return True

    # `_tick` needs `seats_with_execute` / `get_seat_holder` — bypass via
    # the lane-eligibility cache by monkey-patching imports lazily.
    import shared.executor_seat as es
    monkeypatch.setattr(es, "seats_with_execute", lambda lane: ["equity_executor"])

    async def fake_get_holder(name): return "barracuda"
    monkeypatch.setattr(es, "get_seat_holder", fake_get_holder)

    await auto_router._tick()

    # Exactly one writeback to mark the blocked intent terminal.
    assert any(
        u.get("$set", {}).get("gate_state") == "blocked"
        and q.get("intent_id") == "I1"
        for q, u in updates
    ), (
        f"Expected gate_state=blocked writeback for I1; got updates={updates!r}. "
        "Without this, the same TRIPWIRE-class intent loops through the auto-"
        "router every 30s forever and crushes the funnel."
    )


@pytest.mark.asyncio
async def test_executed_verdict_skips_writeback(monkeypatch):
    """An EXECUTED verdict has already had its gate_state stamped by
    `execution_submit`. The auto-router must NOT double-write —
    otherwise we'd overwrite `gate_state=passed` with `blocked`."""
    from shared import auto_router

    updates: list[tuple[dict, dict]] = []

    class FakeColl:
        async def update_one(self, q, u):
            updates.append((q, u))

        def find(self, *args, **kwargs):
            class _C:
                def sort(self_, *a, **k): return self_
                async def to_list(self_, *a, **k):
                    return [{
                        "intent_id": "I2",
                        "lane": "equity",
                        "stack": "barracuda",
                        "symbol": "AAPL",
                        "action": "BUY",
                    }]
            return _C()

    class FakeDB:
        def __getitem__(self, name):
            return FakeColl()

    monkeypatch.setattr(auto_router, "db", FakeDB())

    async def fake_route_one(intent):
        return {"intent_id": intent.get("intent_id"), "verdict": "executed"}

    monkeypatch.setattr(auto_router, "_route_one", fake_route_one)
    monkeypatch.setattr(auto_router, "_sweep_seat_mismatched_intents",
                        lambda: _async_zero())

    import shared.executor_seat as es
    monkeypatch.setattr(es, "seats_with_execute", lambda lane: ["equity_executor"])

    async def fake_get_holder(name): return "barracuda"
    monkeypatch.setattr(es, "get_seat_holder", fake_get_holder)

    await auto_router._tick()

    # No terminal-state writeback for an executed intent.
    assert not any(
        u.get("$set", {}).get("gate_state") in {"blocked", "advisory_only"}
        for _q, u in updates
    ), (
        "Auto-router must NOT writeback gate_state when the pipeline "
        "verdict was 'executed' — execution_submit owns that field."
    )


async def _async_zero() -> int:
    return 0
