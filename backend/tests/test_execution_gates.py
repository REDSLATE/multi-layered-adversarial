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
    }


def _patches(*, holder, broker_connected, daily_spend, lane="equity"):
    """Canonical patch set for the gate chain.

    The position-model lookup queries `seats_with_execute(lane)` to find
    eligible seats, then iterates calling `get_seat_holder(seat)`. Patch
    BOTH so the test controls who's "currently in the seat" without
    touching the live DB.
    """
    eligible_seats = ["executor"] if lane == "equity" else ["crypto"]

    async def _holder_lookup(seat: str):
        return holder if seat in eligible_seats else None

    broker_mock = AsyncMock(return_value=object() if broker_connected else None)
    return [
        patch("shared.executor_seat.get_seat_holder", new=AsyncMock(side_effect=_holder_lookup)),
        patch("shared.executor_seat.seats_with_execute", new=lambda _lane: eligible_seats),
        patch("shared.execution.get_alpaca_adapter", new=broker_mock),
        patch("shared.exposure_caps.get_alpaca_adapter", new=AsyncMock(return_value=None)),
        patch("shared.exposure_caps.daily_spend_usd", new=AsyncMock(return_value=daily_spend)),
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
    """Patent-suspension (2026-02-17): broker_connected is suspended.
    The gate still RUNS and records the doctrine_reason but is force-
    passed with `suspended=True`. Seat policy is the only authoritative
    gate while the suspension flag is active."""
    intent = _intent()
    with _enter_all(_patches(holder="camaro", broker_connected=False, daily_spend=0.0)):
        res = await _evaluate_gates(intent, order_notional_usd=10.0)
    bg = next(g for g in res["gates"] if g["name"] == "broker_connected")
    assert bg["passed"] is True, "broker_connected must be suspended under PATENT_SUSPENSION_ACTIVE"
    assert bg.get("suspended") is True
    assert bg.get("doctrine_reason"), "doctrine_reason should preserve what would have blocked"


@pytest.mark.asyncio
async def test_gate_chain_blocks_when_daily_cap_would_be_breached():
    """Patent-suspension (2026-02-17): cap_per_day is suspended.
    The cap still RUNS and surfaces the doctrine_reason but never
    blocks. Operator accepted the risk of suspending exposure caps."""
    intent = _intent()
    near_cap = caps_mod.CAP_PER_DAY_USD - 100.0
    with _enter_all(_patches(holder="camaro", broker_connected=True, daily_spend=near_cap)):
        res = await _evaluate_gates(intent, order_notional_usd=10_000.0)
    daily = next(g for g in res["gates"] if g["name"] == "cap_per_day")
    assert daily["passed"] is True, "cap_per_day must be suspended under PATENT_SUSPENSION_ACTIVE"
    assert daily.get("suspended") is True


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
