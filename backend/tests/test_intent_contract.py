"""Tests for `shared.intent_contract.classify_brain_intent` —
the MC-side classifier that decides whether a brain emission is
an executable candidate or advisory only.

Doctrine-pinned (tripwire): the reason strings are part of the
audit contract. Downstream consumers (ledger, dashboards, promotion
artifact filter) read `classification.reason` exactly as emitted.
"""
from __future__ import annotations

import pytest

from shared.intent_contract import (
    DIRECTIONAL,
    NON_DIRECTIONAL,
    IntentClassification,
    classify_brain_intent,
)

pytestmark = pytest.mark.tripwire


# ─────────────────────── happy path ─────────────────────────


def test_executable_candidate_camaro_crypto_buy():
    c = classify_brain_intent({
        "brain": "camaro",
        "lane": "crypto",
        "symbol": "BTC/USD",
        "direction": "BUY",
        "raw_confidence": 0.62,
    }, min_exec_conf=0.30)
    assert c.executable_candidate is True
    assert c.advisory_only is False
    assert c.reason == "EXECUTABLE_CANDIDATE"
    assert c.normalized_direction == "BUY"
    assert c.confidence == 0.62
    assert c.lane == "crypto"
    assert c.symbol == "BTC/USD"
    assert c.brain == "camaro"


def test_executable_candidate_alpha_equity_sell_with_action_field():
    """Older intent shape uses `action` instead of `direction`."""
    c = classify_brain_intent({
        "source": "alpha",  # falls back from `brain`
        "lane": "equity",
        "symbol": "AAPL",
        "action": "SELL",
        "confidence": 0.55,
    })
    assert c.executable_candidate is True
    assert c.normalized_direction == "SELL"
    assert c.brain == "alpha"
    assert c.confidence == 0.55


# ─────────────────────── advisory-only paths ─────────────────────


def test_advisory_hold_action_is_non_directional_opinion():
    c = classify_brain_intent({
        "brain": "camaro",
        "lane": "crypto",
        "symbol": "BTC/USD",
        "action": "HOLD",
        "confidence": 0.9,
    })
    assert c.advisory_only is True
    assert c.reason == "NON_DIRECTIONAL_OPINION"
    assert c.normalized_direction == "HOLD"


def test_advisory_empty_direction_is_non_directional():
    c = classify_brain_intent({
        "brain": "redeye",
        "lane": "crypto",
        "symbol": "ETH/USD",
        "direction": "",
        "confidence": 0.8,
    })
    assert c.advisory_only is True
    assert c.reason == "NON_DIRECTIONAL_OPINION"


def test_advisory_wait_neutral_none_treated_as_non_directional():
    for d in ("WAIT", "NEUTRAL", "NONE"):
        c = classify_brain_intent({
            "brain": "redeye", "lane": "crypto", "symbol": "ETH/USD",
            "direction": d, "raw_confidence": 0.8,
        })
        assert c.advisory_only is True
        assert c.reason == "NON_DIRECTIONAL_OPINION", f"{d} should be non-directional"


def test_advisory_unknown_direction_carries_value_in_reason():
    c = classify_brain_intent({
        "brain": "alpha", "lane": "equity", "symbol": "AAPL",
        "direction": "PONDER", "raw_confidence": 0.6,
    })
    assert c.advisory_only is True
    assert c.reason == "UNKNOWN_DIRECTION:PONDER"


def test_advisory_missing_symbol():
    c = classify_brain_intent({
        "brain": "camaro", "lane": "crypto",
        "direction": "BUY", "raw_confidence": 0.6,
    })
    assert c.advisory_only is True
    assert c.reason == "SYMBOL_MISSING"


def test_advisory_blank_symbol_treated_as_missing():
    c = classify_brain_intent({
        "brain": "camaro", "lane": "crypto", "symbol": "   ",
        "direction": "BUY", "raw_confidence": 0.6,
    })
    assert c.advisory_only is True
    assert c.reason == "SYMBOL_MISSING"


def test_advisory_below_exec_floor():
    c = classify_brain_intent({
        "brain": "camaro", "lane": "crypto", "symbol": "BTC/USD",
        "direction": "BUY", "raw_confidence": 0.10,
    }, min_exec_conf=0.30)
    assert c.advisory_only is True
    assert c.reason == "CONFIDENCE_BELOW_EXEC_FLOOR"


def test_advisory_lane_missing():
    c = classify_brain_intent({
        "brain": "camaro", "symbol": "BTC/USD",
        "direction": "BUY", "raw_confidence": 0.6,
    })
    assert c.advisory_only is True
    assert c.reason == "LANE_MISSING_OR_INVALID"


def test_advisory_lane_invalid_value():
    c = classify_brain_intent({
        "brain": "camaro", "lane": "options",
        "symbol": "AAPL", "direction": "BUY", "raw_confidence": 0.6,
    })
    assert c.advisory_only is True
    assert c.reason == "LANE_MISSING_OR_INVALID"


# ─────────────────────── field fallback chain ─────────────────────


def test_confidence_fallback_chain():
    """raw_confidence > confidence > effective_confidence > 0.0"""
    base = {"brain": "x", "lane": "equity", "symbol": "AAPL", "direction": "BUY"}
    assert classify_brain_intent({**base, "raw_confidence": 0.9, "confidence": 0.1}).confidence == 0.9
    assert classify_brain_intent({**base, "confidence": 0.5}).confidence == 0.5
    assert classify_brain_intent({**base, "effective_confidence": 0.4}).confidence == 0.4
    # No confidence field at all → 0.0 → CONFIDENCE_BELOW_EXEC_FLOOR
    c = classify_brain_intent({**base})
    assert c.advisory_only is True
    assert c.reason == "CONFIDENCE_BELOW_EXEC_FLOOR"


def test_confidence_non_numeric_coerces_to_zero():
    c = classify_brain_intent({
        "brain": "x", "lane": "equity", "symbol": "AAPL",
        "direction": "BUY", "raw_confidence": "not-a-number",
    })
    assert c.confidence == 0.0
    assert c.advisory_only is True
    assert c.reason == "CONFIDENCE_BELOW_EXEC_FLOOR"


def test_brain_fallback_to_source():
    c = classify_brain_intent({
        "source": "REDEYE", "lane": "crypto", "symbol": "BTC/USD",
        "direction": "BUY", "raw_confidence": 0.6,
    })
    assert c.brain == "redeye"  # lowercased


def test_symbol_fallback_to_canonical_id():
    c = classify_brain_intent({
        "brain": "x", "lane": "equity", "canonical_id": "EQ:AAPL",
        "direction": "BUY", "raw_confidence": 0.6,
    })
    assert c.symbol == "EQ:AAPL"


def test_classification_is_frozen_dataclass():
    c = classify_brain_intent({
        "brain": "x", "lane": "equity", "symbol": "AAPL",
        "direction": "BUY", "raw_confidence": 0.6,
    })
    assert isinstance(c, IntentClassification)
    with pytest.raises((AttributeError, TypeError)):  # frozen
        c.reason = "MUTATED"


# ─────────────────────── doctrine sets stability ────────────


def test_directional_set_is_buy_sell_only():
    assert DIRECTIONAL == {"BUY", "SELL"}


def test_non_directional_set_contents():
    assert NON_DIRECTIONAL == {"HOLD", "WAIT", "NONE", "NEUTRAL", ""}
