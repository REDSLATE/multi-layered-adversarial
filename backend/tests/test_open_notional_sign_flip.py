"""Regression tests for the open-notional sign-flip bug.

Doctrine pin (2026-06-10, recurring P1 finally fixed):
Before this fix, `evaluate_open_notional(side="BUY")` always added
the order's notional to projected open exposure — regardless of
whether the BUY was opening a long or COVERING a short. The
symmetric inversion bit SHORT positions: a SELL against an existing
SHORT (which is an ADD) was treated as closing.

These tests pin the position-evolution-aware behavior:
  * OPEN/ADD/FLIP/SCALE_IN → grows open notional
  * REDUCE/CLOSE/PARTIAL_COVER/FULL_COVER/SCALE_OUT/HOLD → no growth
  * Unknown / missing evolution → falls back to side heuristic
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import exposure_caps as caps  # noqa: E402


@pytest.fixture
def _patch_open_notional():
    """Stub out the live `open_notional_usd()` so we control current
    exposure in the test."""
    with patch("shared.exposure_caps.open_notional_usd",
               new=AsyncMock(return_value=100.0)):
        yield


@pytest.mark.asyncio
async def test_buy_to_cover_short_does_NOT_grow_open_notional(_patch_open_notional):
    """The bug: BUY-to-COVER was counted as opening exposure.
    Fix: passing `position_evolution='close'` correctly classifies
    it as shrinking, so projected stays at current."""
    ev = await caps.evaluate_open_notional(
        order_notional_usd=50.0,
        side="BUY",
        position_evolution="close",  # buying-to-close a short
    )
    assert ev.projected_usd == 100.0, (
        f"BUY-to-COVER must not grow open notional; "
        f"projected={ev.projected_usd}"
    )
    assert "no growth" in ev.reason


@pytest.mark.asyncio
async def test_sell_to_add_short_DOES_grow_open_notional(_patch_open_notional):
    """Symmetric: SELL against an existing SHORT is an ADD —
    it MUST grow projected open notional."""
    ev = await caps.evaluate_open_notional(
        order_notional_usd=50.0,
        side="SELL",
        position_evolution="add",  # adding to an existing short
    )
    assert ev.projected_usd == 150.0, (
        f"SELL-to-ADD-SHORT must grow open notional; "
        f"projected={ev.projected_usd}"
    )
    assert "grows" in ev.reason


@pytest.mark.asyncio
async def test_buy_to_open_long_grows_open_notional(_patch_open_notional):
    ev = await caps.evaluate_open_notional(
        order_notional_usd=50.0,
        side="BUY",
        position_evolution="open",
    )
    assert ev.projected_usd == 150.0
    assert "grows" in ev.reason


@pytest.mark.asyncio
async def test_sell_to_close_long_no_growth(_patch_open_notional):
    ev = await caps.evaluate_open_notional(
        order_notional_usd=50.0,
        side="SELL",
        position_evolution="close",
    )
    assert ev.projected_usd == 100.0
    assert "no growth" in ev.reason


@pytest.mark.asyncio
async def test_flip_grows_open_notional(_patch_open_notional):
    """A FLIP crosses through zero — the post-flip leg is full new
    exposure on the opposite side, so it must count as growing."""
    ev = await caps.evaluate_open_notional(
        order_notional_usd=50.0,
        side="SELL",
        position_evolution="flip",
    )
    assert ev.projected_usd == 150.0
    assert "grows" in ev.reason


@pytest.mark.asyncio
async def test_scale_in_grows_scale_out_does_not(_patch_open_notional):
    grow = await caps.evaluate_open_notional(
        order_notional_usd=20.0, side="BUY", position_evolution="scale_in",
    )
    shrink = await caps.evaluate_open_notional(
        order_notional_usd=20.0, side="SELL", position_evolution="scale_out",
    )
    assert grow.projected_usd == 120.0
    assert shrink.projected_usd == 100.0


@pytest.mark.asyncio
async def test_hold_never_grows(_patch_open_notional):
    ev = await caps.evaluate_open_notional(
        order_notional_usd=999.0, side="BUY", position_evolution="hold",
    )
    assert ev.projected_usd == 100.0


@pytest.mark.asyncio
async def test_legacy_caller_without_evolution_falls_back_to_side(_patch_open_notional):
    """Callers that don't yet pass position_evolution get the legacy
    side-only heuristic — buggy for COVERs but still correct for the
    dominant flat-position case. Maintains backward compatibility."""
    ev = await caps.evaluate_open_notional(
        order_notional_usd=50.0, side="BUY",
    )
    assert ev.projected_usd == 150.0
    assert "side-only" in ev.reason


@pytest.mark.asyncio
async def test_unknown_evolution_falls_back_to_side(_patch_open_notional):
    """An evolution value the validator doesn't recognize must NOT
    silently flip the classification. Fall back to side heuristic."""
    ev = await caps.evaluate_open_notional(
        order_notional_usd=50.0, side="BUY",
        position_evolution="some_future_unknown_state",
    )
    # Side BUY → opening under legacy heuristic
    assert ev.projected_usd == 150.0


@pytest.mark.asyncio
async def test_evaluate_all_propagates_position_evolution(_patch_open_notional):
    """`evaluate_all` must forward `position_evolution` into the
    open-notional check. If it didn't, callers upgrading wouldn't see
    the fix until they switched away from `evaluate_all`."""
    with patch("shared.exposure_caps.daily_spend_usd",
               new=AsyncMock(return_value=0.0)):
        evs = await caps.evaluate_all(
            order_notional_usd=50.0,
            side="BUY",
            lane="equity",
            position_evolution="close",  # buying-to-cover-short
        )
    open_eval = next(e for e in evs if e.name == "cap_open_notional")
    assert open_eval.projected_usd == 100.0
    assert "no growth" in open_eval.reason
