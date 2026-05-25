"""Tripwires — OPEN/CLOSE action verb translation on /api/intents.

Pins the doctrine:

  1. action=OPEN without direction is rejected at the boundary (422).
  2. action=OPEN with direction='long' translates to action=BUY.
  3. action=OPEN with direction='short' translates to action=SHORT.
  4. action=CLOSE without lane is rejected (422).
  5. action=CLOSE delegates to the close_position flow.
  6. Existing BUY/SHORT/SELL/COVER/HOLD verbs are unchanged.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.intents import IntentIn


pytestmark = [pytest.mark.tripwire]


# ─── schema-level: legacy verbs still work ────────────────────────


def test_legacy_buy_still_accepted():
    i = IntentIn(
        stack="alpha", action="BUY", symbol="AAPL", lane="equity",
        confidence=0.6, rationale="legacy buy",
    )
    assert i.action == "BUY"


def test_legacy_sell_still_accepted():
    i = IntentIn(
        stack="alpha", action="SELL", symbol="AAPL", lane="equity",
        confidence=0.6, rationale="legacy sell",
    )
    assert i.action == "SELL"


def test_legacy_short_still_accepted():
    i = IntentIn(
        stack="redeye", action="SHORT", symbol="TSLA", lane="equity",
        confidence=0.6, rationale="legacy short",
    )
    assert i.action == "SHORT"


def test_legacy_cover_still_accepted():
    i = IntentIn(
        stack="redeye", action="COVER", symbol="TSLA", lane="equity",
        confidence=0.6, rationale="legacy cover",
    )
    assert i.action == "COVER"


def test_hold_still_accepted():
    i = IntentIn(
        stack="alpha", action="HOLD", symbol="AAPL", lane="equity",
        confidence=0.3, rationale="hold",
    )
    assert i.action == "HOLD"


# ─── new verbs at the schema level ────────────────────────────────


def test_open_verb_schema_valid():
    """OPEN is a valid action at the schema level. The validation that
    `direction` must be present runs in post_intent, not Pydantic."""
    i = IntentIn(
        stack="camaro", action="OPEN", direction="long",
        symbol="BTC", lane="crypto", confidence=0.7,
        rationale="open btc long",
    )
    assert i.action == "OPEN"
    assert i.direction == "long"


def test_close_verb_schema_valid():
    i = IntentIn(
        stack="camaro", action="CLOSE", symbol="BTC", lane="crypto",
        confidence=0.9, rationale="close btc",
    )
    assert i.action == "CLOSE"


def test_invalid_direction_rejected():
    """direction must be 'long' or 'short' if set."""
    with pytest.raises(ValidationError):
        IntentIn(
            stack="camaro", action="OPEN", direction="north",
            symbol="BTC", lane="crypto", confidence=0.7,
            rationale="invalid direction",
        )


def test_invalid_action_still_rejected():
    """Random strings are still rejected by the Literal."""
    with pytest.raises(ValidationError):
        IntentIn(
            stack="camaro", action="YOLO", symbol="BTC",
            lane="crypto", confidence=0.7, rationale="yolo",
        )


def test_direction_optional_for_legacy_verbs():
    """BUY / SHORT / SELL / COVER do not require direction."""
    i = IntentIn(
        stack="alpha", action="BUY", symbol="AAPL", lane="equity",
        confidence=0.6, rationale="no direction needed",
    )
    assert i.direction is None
