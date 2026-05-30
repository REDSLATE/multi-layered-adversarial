"""Unit tests for the gate-chain in shared/execution.py."""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from shared import exposure_caps as caps_mod
from shared.execution import _evaluate_gates


def test_per_order_cap_blocks_above_threshold():
    e = caps_mod.evaluate_per_order(11.0)
    assert e.passed is False
    assert "exceeds" in e.reason


def test_per_order_cap_passes_at_threshold():
    e = caps_mod.evaluate_per_order(10.0)
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
        "executor_holder_at_post": posted_under if posted_under is not None else (stack if holds else None),
    }


def test_gate_chain_blocks_when_executor_seat_empty():
    intent = _intent(holds=False, posted_under=None)
    with patch("shared.execution.get_executor_holder", new=AsyncMock(return_value=None)), \
         patch("shared.execution.get_alpaca_adapter", new=AsyncMock(return_value=object())), \
         patch("shared.exposure_caps.get_alpaca_adapter", new=AsyncMock(return_value=None)), \
         patch("shared.exposure_caps.daily_spend_usd", new=AsyncMock(return_value=0.0)):
        res = asyncio.run(_evaluate_gates(intent, order_notional_usd=10.0))
    assert res["verdict"] == "would_block"
    seat = next(g for g in res["gates"] if g["name"] == "executor_seat_check")
    assert seat["passed"] is False


def test_gate_chain_passes_when_everything_aligned():
    intent = _intent()
    with patch("shared.execution.get_executor_holder", new=AsyncMock(return_value="camaro")), \
         patch("shared.execution.get_alpaca_adapter", new=AsyncMock(return_value=object())), \
         patch("shared.exposure_caps.get_alpaca_adapter", new=AsyncMock(return_value=None)), \
         patch("shared.exposure_caps.daily_spend_usd", new=AsyncMock(return_value=0.0)):
        res = asyncio.run(_evaluate_gates(intent, order_notional_usd=10.0))
    assert res["verdict"] == "would_pass", res["gates"]
    for g in res["gates"]:
        assert g["passed"] is True, (g["name"], g["reason"])


def test_gate_chain_blocks_when_broker_disconnected():
    intent = _intent()
    with patch("shared.execution.get_executor_holder", new=AsyncMock(return_value="camaro")), \
         patch("shared.execution.get_alpaca_adapter", new=AsyncMock(return_value=None)), \
         patch("shared.exposure_caps.get_alpaca_adapter", new=AsyncMock(return_value=None)), \
         patch("shared.exposure_caps.daily_spend_usd", new=AsyncMock(return_value=0.0)):
        res = asyncio.run(_evaluate_gates(intent, order_notional_usd=10.0))
    assert res["verdict"] == "would_block"
    bg = next(g for g in res["gates"] if g["name"] == "broker_connected")
    assert bg["passed"] is False


def test_gate_chain_blocks_when_daily_cap_would_be_breached():
    intent = _intent()
    with patch("shared.execution.get_executor_holder", new=AsyncMock(return_value="camaro")), \
         patch("shared.execution.get_alpaca_adapter", new=AsyncMock(return_value=object())), \
         patch("shared.exposure_caps.get_alpaca_adapter", new=AsyncMock(return_value=None)), \
         patch("shared.exposure_caps.daily_spend_usd", new=AsyncMock(return_value=48.0)):
        res = asyncio.run(_evaluate_gates(intent, order_notional_usd=10.0))
    daily = next(g for g in res["gates"] if g["name"] == "cap_per_day")
    assert daily["passed"] is False
    assert res["verdict"] == "would_block"


def test_hold_action_not_routable():
    intent = _intent(action="HOLD")
    with patch("shared.execution.get_executor_holder", new=AsyncMock(return_value="camaro")), \
         patch("shared.execution.get_alpaca_adapter", new=AsyncMock(return_value=object())), \
         patch("shared.exposure_caps.get_alpaca_adapter", new=AsyncMock(return_value=None)), \
         patch("shared.exposure_caps.daily_spend_usd", new=AsyncMock(return_value=0.0)):
        res = asyncio.run(_evaluate_gates(intent, order_notional_usd=10.0))
    routable = next(g for g in res["gates"] if g["name"] == "action_routable")
    assert routable["passed"] is False
    assert res["verdict"] == "would_block"


def test_seat_rotation_does_not_block_under_position_model():
    """Position-model doctrine (2026-05-28): authority lives in the
    seat, not the brain that posted. An intent posted while Camaro
    held the seat MUST still execute after the operator rotates to
    Alpha — because Alpha now holds executor authority and the intent
    is for an equity lane that Alpha's seat permits.
    """
    intent = _intent(stack="camaro", holds=True, posted_under="camaro")
    with patch("shared.executor_seat.get_executor_holder", new=AsyncMock(return_value="alpha")), \
         patch("shared.execution.get_alpaca_adapter", new=AsyncMock(return_value=object())), \
         patch("shared.exposure_caps.get_alpaca_adapter", new=AsyncMock(return_value=None)), \
         patch("shared.exposure_caps.daily_spend_usd", new=AsyncMock(return_value=0.0)):
        res = asyncio.run(_evaluate_gates(intent, order_notional_usd=10.0))
    seat = next(g for g in res["gates"] if g["name"] == "executor_seat_check")
    assert seat["passed"] is True, seat["reason"]
    assert "alpha" in seat["reason"].lower()
    assert "position-model" in seat["reason"].lower()
