"""Unit tests for the gate-chain in `shared/execution.py`.

Doctrine pin (2026-05-31):
  - Seat authority lookup goes through `shared.executor_seat.get_seat_holder`
    (not the legacy `shared.execution.get_executor_holder` which was
    renamed during the position-model refactor).
  - Per-order / per-day caps live in `shared.exposure_caps` and are
    PAPER-TRADING-PERMISSIVE since 2026-05-14 (CAP_PER_ORDER_USD = 100k,
    CAP_PER_DAY_USD = 1M). Tests use the module constants directly so
    they survive future cap changes.
"""
import pytest
from unittest.mock import AsyncMock, patch

from shared import exposure_caps as caps_mod
from shared.execution import _evaluate_gates


def test_per_order_cap_blocks_above_threshold():
    """An order strictly above CAP_PER_ORDER_USD blocks."""
    e = caps_mod.evaluate_per_order(caps_mod.CAP_PER_ORDER_USD + 1.0)
    assert e.passed is False
    assert "exceeds" in e.reason.lower() or "cap" in e.reason.lower()


def test_per_order_cap_passes_at_threshold():
    """An order at exactly CAP_PER_ORDER_USD passes."""
    e = caps_mod.evaluate_per_order(caps_mod.CAP_PER_ORDER_USD)
    assert e.passed is True


def _intent(stack="camaro", action="BUY", holds=True, posted_under=None, lane="equity"):
    return {
        "intent_id": "i",
        "stack": stack,
        "action": action,
        "symbol": "AAPL",
        "lane": lane,
        "may_execute": False,
        "requires_gate_pass": True,
        "holds_executor_seat": holds,
        "executor_holder_at_post": (
            posted_under if posted_under is not None else (stack if holds else None)
        ),
        # 2026-06-10: the roadguard_spread_floor gate (added 2026-05-27)
        # requires `snapshot.spread_bps`. Provide a tight 5bps so equity
        # intents clear the 50bps cap and crypto intents clear the 200bps
        # cap — both well within bounds.
        "snapshot": {"spread_bps": 5.0},
    }


def _patches(*, holder, broker_connected, daily_spend, lane="equity", lane_enabled=True):
    """Canonical patch set for the gate chain.

    The position-model lookup queries `seats_with_execute(lane)` to find
    eligible seats, then iterates calling `get_seat_holder(seat)`. Patch
    BOTH so the test controls who's "currently in the seat" without
    touching the live DB.

    `lane_enabled` (default True) patches the lane-execution toggle so
    tests don't need to seed the `shared_lane_execution_toggles` doc.

    Doctrine (2026-06-10, post-$500-AAPL re-arm): the `broker_connected`
    gate now uses `shared.broker_router.adapter_for_lane(lane)` when the
    intent has a lane (which all canonical intents do). Patch THAT —
    the legacy `get_alpaca_adapter` is only consulted for lane-less
    intents. We keep the legacy patch too so this set works for both.
    """
    eligible_seats = ["executor"] if lane == "equity" else ["crypto"]

    async def _holder_lookup(seat: str):
        return holder if seat in eligible_seats else None

    # Minimal adapter shape — `adapter_for_lane` returns an adapter
    # whose `.name` is read in the gate's reason string.
    class _FakeAdapter:
        name = "fake-broker"

    fake_adapter = _FakeAdapter() if broker_connected else None
    broker_lane_mock = AsyncMock(return_value=fake_adapter)
    broker_legacy_mock = AsyncMock(return_value=fake_adapter)
    return [
        patch("shared.executor_seat.get_seat_holder", new=AsyncMock(side_effect=_holder_lookup)),
        patch("shared.executor_seat.seats_with_execute", new=lambda _lane: eligible_seats),
        patch("shared.broker_router.adapter_for_lane", new=broker_lane_mock),
        patch("shared.execution.get_alpaca_adapter", new=broker_legacy_mock),
        patch("shared.exposure_caps.get_alpaca_adapter", new=AsyncMock(return_value=None)),
        patch("shared.exposure_caps.daily_spend_usd", new=AsyncMock(return_value=daily_spend)),
        patch("shared.lane_execution.is_lane_execution_enabled", new=AsyncMock(return_value=lane_enabled)),
    ]


def _enter_all(ctx_managers):
    from contextlib import ExitStack
    stack = ExitStack()
    for cm in ctx_managers:
        stack.enter_context(cm)
    return stack


@pytest.mark.asyncio
async def test_gate_chain_blocks_when_executor_seat_empty():
    intent = _intent(holds=False, posted_under=None)
    with _enter_all(_patches(holder=None, broker_connected=True, daily_spend=0.0)):
        res = await _evaluate_gates(intent, order_notional_usd=10.0)
    assert res["verdict"] == "would_block"
    seat = next(g for g in res["gates"] if g["name"] == "executor_seat_check")
    assert seat["passed"] is False


@pytest.mark.asyncio
async def test_gate_chain_passes_when_everything_aligned():
    intent = _intent()
    with _enter_all(_patches(holder="camaro", broker_connected=True, daily_spend=0.0)):
        res = await _evaluate_gates(intent, order_notional_usd=10.0)
    assert res["verdict"] == "would_pass", res["gates"]
    for g in res["gates"]:
        assert g["passed"] is True, (g["name"], g["reason"])


@pytest.mark.asyncio
async def test_gate_chain_blocks_when_broker_disconnected():
    """Doctrine flip (2026-06-09, post-$500-AAPL re-arm):
    `PATENT_SUSPENSION_ACTIVE` was set back to False after a $500
    AAPL order slipped through the $25 cap. With suspension OFF the
    broker_connected gate is once again authoritative — when the
    broker is missing the gate BLOCKS, not suspends.
    """
    intent = _intent()
    with _enter_all(_patches(holder="camaro", broker_connected=False, daily_spend=0.0)):
        res = await _evaluate_gates(intent, order_notional_usd=10.0)
    bg = next(g for g in res["gates"] if g["name"] == "broker_connected")
    assert bg["passed"] is False, "broker_connected must enforce now that patent-suspension is OFF"
    assert bg.get("suspended") is not True
    assert res["verdict"] == "would_block"


@pytest.mark.asyncio
async def test_gate_chain_blocks_when_daily_cap_would_be_breached():
    """Doctrine flip (2026-06-09): exposure caps are authoritative
    again. An order that would push us past `CAP_PER_DAY_USD` must
    BLOCK, not be force-passed."""
    intent = _intent()
    near_cap = caps_mod.CAP_PER_DAY_USD - 100.0
    with _enter_all(_patches(holder="camaro", broker_connected=True, daily_spend=near_cap)):
        res = await _evaluate_gates(intent, order_notional_usd=10_000.0)
    daily = next(g for g in res["gates"] if g["name"] == "cap_per_day")
    assert daily["passed"] is False, "cap_per_day must enforce now that patent-suspension is OFF"
    assert daily.get("suspended") is not True
    assert res["verdict"] == "would_block"


@pytest.mark.asyncio
async def test_hold_action_not_routable():
    intent = _intent(action="HOLD")
    with _enter_all(_patches(holder="camaro", broker_connected=True, daily_spend=0.0)):
        res = await _evaluate_gates(intent, order_notional_usd=10.0)
    routable = next(g for g in res["gates"] if g["name"] == "action_routable")
    assert routable["passed"] is False
    assert res["verdict"] == "would_block"


@pytest.mark.asyncio
async def test_seat_rotation_does_not_block_under_position_model():
    """Position-model doctrine (2026-05-28): authority lives in the
    seat, not the brain that posted. An intent posted while Camaro
    held the seat MUST still execute after the operator rotates to
    Alpha — because Alpha now holds executor authority and the intent
    is for an equity lane that Alpha's seat permits.
    """
    intent = _intent(stack="camaro", holds=True, posted_under="camaro")
    with _enter_all(_patches(holder="alpha", broker_connected=True, daily_spend=0.0)):
        res = await _evaluate_gates(intent, order_notional_usd=10.0)
    seat = next(g for g in res["gates"] if g["name"] == "executor_seat_check")
    assert seat["passed"] is True, seat["reason"]
    assert "alpha" in seat["reason"].lower()
    assert "position-model" in seat["reason"].lower()
