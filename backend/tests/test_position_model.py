"""Tests for the position-aware intent classifier.

Pins the doctrine from the 2026-06-09 AAPL misread incident:

    SELL when flat  = open short
    SELL when long  = reduce / close long
    BUY  when short = cover short (== reduce / close)
    BUY  when flat  = open long
    BUY  when long  = add to long
    SELL when short = add to short
    Any cross-through-zero = FLIP

Plus the misread-detection logic that would have caught the AAPL
incident (brain assumed FLAT, broker was SHORT, brain emitted BUY).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from shared.position_model import (
    ACTION_BUY,
    ACTION_SELL,
    IntentType,
    PositionMisread,
    PositionSide,
    PositionState,
    classify_intent,
    detect_misread,
)


def _pos(symbol: str = "AAPL", signed: float = 0.0) -> PositionState:
    return PositionState(symbol=symbol, signed_qty=signed)


# ── PositionState.side derivation ─────────────────────────────────


def test_side_from_signed_qty():
    assert _pos(signed=0).side == PositionSide.FLAT
    assert _pos(signed=10).side == PositionSide.LONG
    assert _pos(signed=-10).side == PositionSide.SHORT
    # Floating-point dust around zero counts as FLAT
    assert _pos(signed=1e-10).side == PositionSide.FLAT


# ── Eight canonical transition rules (operator-stated doctrine) ───


@pytest.mark.parametrize(
    "action,current_signed,qty,expected",
    [
        # FLAT → OPEN
        (ACTION_BUY,  0,    5,  IntentType.OPEN),    # flat + BUY  = open long
        (ACTION_SELL, 0,    5,  IntentType.OPEN),    # flat + SELL = open short
        # Same-side ADD
        (ACTION_BUY,  10,   5,  IntentType.ADD),     # long + BUY  = add to long
        (ACTION_SELL, -10,  5,  IntentType.ADD),     # short + SELL = add to short
        # Opposite-side REDUCE (doesn't cross zero)
        (ACTION_SELL, 10,   3,  IntentType.REDUCE),  # long + SELL 3 = reduce
        (ACTION_BUY,  -10,  3,  IntentType.REDUCE),  # short + BUY 3 = reduce (cover partial)
        # Opposite-side CLOSE (exactly to zero)
        (ACTION_SELL, 10,   10, IntentType.CLOSE),   # long + SELL all = close
        (ACTION_BUY,  -10,  10, IntentType.CLOSE),   # short + BUY all = cover-to-flat (THE AAPL FIX)
        # Opposite-side FLIP (crosses through zero)
        (ACTION_SELL, 10,   15, IntentType.FLIP),    # long 10 + SELL 15 = short 5
        (ACTION_BUY,  -10,  15, IntentType.FLIP),    # short 10 + BUY 15 = long 5
    ],
)
def test_classify_intent_covers_all_eight_rules(action, current_signed, qty, expected):
    current = _pos(signed=current_signed)
    assert classify_intent(action, qty, current) == expected


def test_zero_qty_raises():
    with pytest.raises(ValueError, match="must be > 0"):
        classify_intent(ACTION_BUY, 0, _pos(signed=0))


def test_unknown_action_raises():
    with pytest.raises(ValueError, match="unknown action"):
        classify_intent("HOVER", 5, _pos(signed=0))


# ── Misread detection — the AAPL scenario ────────────────────────


def test_aapl_misread_2026_06_09_is_caught():
    """The exact failure mode the operator described: brain assumed
    FLAT, position was actually SHORT, brain emitted BUY. The
    correct intent is REDUCE/CLOSE (cover the short, take profit).
    The brain saw OPEN LONG — opposite direction, opposite risk."""
    actual = PositionState(symbol="AAPL", signed_qty=-1.33)  # short 1.33 sh
    m = detect_misread(
        emitted_action=ACTION_BUY,
        assumed_side=PositionSide.FLAT,  # brain didn't see the short
        actual=actual,
        brain="redeye",
        lane="equity",
        intended_qty=0.1,
        note="2026-06-09 incident — brain saturated BUY AAPL against a short",
    )
    assert m is not None
    assert m.missed_short_profit is True
    assert m.correct_intent_type == IntentType.REDUCE
    assert m.assumed_side == PositionSide.FLAT
    assert m.actual_side == PositionSide.SHORT
    doc = m.to_doc()
    assert doc["kind"] == "MISREAD_POSITION_SIDE"
    assert doc["missed_short_profit"] is True
    assert doc["correct_intent_type"] == "reduce"


def test_no_misread_when_sides_agree():
    actual = PositionState(symbol="NVDA", signed_qty=10)
    m = detect_misread(
        emitted_action=ACTION_BUY,
        assumed_side=PositionSide.LONG,
        actual=actual,
        brain="alpha", lane="equity",
        intended_qty=2,
    )
    assert m is None


def test_misread_when_brain_thinks_long_but_position_is_flat():
    """The other direction: brain thinks it holds X, position is
    flat (e.g. broker auto-closed a stale leg). A BUY emitted by
    the brain that thinks it's adding to a long is actually
    OPENING fresh — different risk profile."""
    actual = PositionState(symbol="MSFT", signed_qty=0)
    m = detect_misread(
        emitted_action=ACTION_BUY,
        assumed_side=PositionSide.LONG,
        actual=actual,
        brain="camaro", lane="equity",
        intended_qty=1,
    )
    assert m is not None
    assert m.correct_intent_type == IntentType.OPEN
    assert m.missed_short_profit is False
    assert m.actual_side == PositionSide.FLAT


def test_misread_sell_against_short_is_add_not_open():
    """Brain emits SELL thinking it's opening a short. Position is
    already short. Correct semantic is ADD (size-up), not OPEN.
    Without this distinction the auto-router would apply the
    'open-position' sizing rules instead of the 'add-to-existing'
    sizing rules — they may differ by exposure cap math."""
    actual = PositionState(symbol="TSLA", signed_qty=-2)
    m = detect_misread(
        emitted_action=ACTION_SELL,
        assumed_side=PositionSide.FLAT,
        actual=actual,
        brain="chevelle", lane="equity",
        intended_qty=1,
    )
    assert m is not None
    assert m.correct_intent_type == IntentType.ADD
    # NOT missed_short_profit — this is adding to a short, not
    # missing a cover-profit opportunity.
    assert m.missed_short_profit is False
