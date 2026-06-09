"""Unit tests for the operator-pinned trade-transition layer.

Doctrine pin (2026-06-XX): these tests lock the EXACT behavior the
operator specified in the directive:

    "Stop feeding the brains only action=BUY/SELL. Start feeding
     them position_side, intent_type, exposure_direction."

If any of these assertions break, MC is mis-translating brain
intent. The AAPL misread incident was caused by a missing
side-aware classifier — these tests make sure we never regress.
"""
import sys
import os

# Allow `from shared...` imports when pytest runs from /app.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.position_model import (  # noqa: E402
    classify_trade_transition,
    normalize_position,
    allowed_transitions_for,
)


# ── classify_trade_transition: BUY ────────────────────────────────


def test_buy_when_flat_is_open_long():
    r = classify_trade_transition("BUY", signed_qty=0, order_qty=10)
    assert r["current_side"] == "FLAT"
    assert r["intent_type"] == "OPEN_LONG"
    assert r["order_action"] == "BUY"


def test_buy_when_long_is_add_long():
    r = classify_trade_transition("BUY", signed_qty=50, order_qty=10)
    assert r["current_side"] == "LONG"
    assert r["intent_type"] == "ADD_LONG"


def test_buy_when_short_partial_is_reduce_short():
    # AAPL incident shape: short 100, BUY 60 → REDUCE_SHORT (a cover).
    r = classify_trade_transition("BUY", signed_qty=-100, order_qty=60)
    assert r["current_side"] == "SHORT"
    assert r["intent_type"] == "REDUCE_SHORT"


def test_buy_when_short_exact_is_close_short():
    r = classify_trade_transition("BUY", signed_qty=-100, order_qty=100)
    assert r["intent_type"] == "CLOSE_SHORT"


def test_buy_when_short_over_is_flip_to_long():
    r = classify_trade_transition("BUY", signed_qty=-100, order_qty=150)
    assert r["intent_type"] == "FLIP_SHORT_TO_LONG"


# ── classify_trade_transition: SELL ───────────────────────────────


def test_sell_when_flat_is_open_short():
    r = classify_trade_transition("SELL", signed_qty=0, order_qty=10)
    assert r["intent_type"] == "OPEN_SHORT"


def test_sell_when_short_is_add_short():
    r = classify_trade_transition("SELL", signed_qty=-50, order_qty=10)
    assert r["intent_type"] == "ADD_SHORT"


def test_sell_when_long_partial_is_reduce_long():
    r = classify_trade_transition("SELL", signed_qty=100, order_qty=60)
    assert r["intent_type"] == "REDUCE_LONG"


def test_sell_when_long_exact_is_close_long():
    r = classify_trade_transition("SELL", signed_qty=100, order_qty=100)
    assert r["intent_type"] == "CLOSE_LONG"


def test_sell_when_long_over_is_flip_to_short():
    r = classify_trade_transition("SELL", signed_qty=100, order_qty=150)
    assert r["intent_type"] == "FLIP_LONG_TO_SHORT"


def test_hold_returns_hold_intent():
    r = classify_trade_transition("HOLD", signed_qty=-5, order_qty=0)
    assert r["intent_type"] == "HOLD"


# ── normalize_position ────────────────────────────────────────────


def test_normalize_short_with_side_label_negates_qty():
    """Broker reports `qty=100, side=SHORT` (Public.com shape).
    Normalizer MUST produce signed_qty=-100 — the AAPL bug was that
    qty was treated as +100 because side was a string label, not a
    sign."""
    r = normalize_position({"symbol": "AAPL", "qty": 100, "side": "SHORT"})
    assert r["signed_qty"] == -100.0
    assert r["side"] == "SHORT"
    assert r["qty_abs"] == 100.0


def test_normalize_long_with_side_label():
    r = normalize_position({"symbol": "MSFT", "qty": 50, "side": "long"})
    assert r["signed_qty"] == 50.0
    assert r["side"] == "LONG"


def test_normalize_sell_short_alias():
    r = normalize_position({"symbol": "TSLA", "qty": 25, "side": "SELL_SHORT"})
    assert r["signed_qty"] == -25.0
    assert r["side"] == "SHORT"


def test_normalize_flat_when_qty_zero():
    r = normalize_position({"symbol": "NVDA", "qty": 0, "side": "long"})
    assert r["side"] == "FLAT"
    assert r["signed_qty"] == 0.0


def test_normalize_signed_qty_passthrough_when_no_side():
    """If broker only sends signed qty with no side label, trust the
    sign on the qty itself."""
    r = normalize_position({"symbol": "BTC/USD", "qty": -1.5})
    assert r["signed_qty"] == -1.5
    assert r["side"] == "SHORT"


def test_normalize_preserves_pnl_and_market_value():
    r = normalize_position({
        "symbol": "MSFT",
        "qty": 3,
        "side": "SHORT",
        "market_value": -1200.0,
        "unrealized_pl": 45.20,
        "avg_entry_price": 400.0,
    })
    assert r["market_value"] == -1200.0
    assert r["unrealized_pl"] == 45.20
    assert r["avg_entry_price"] == 400.0


# ── allowed_transitions_for ───────────────────────────────────────


def test_allowed_transitions_short_includes_buy_to_close():
    """The exact operator-pinned example — a short MSFT position
    must expose BUY_TO_REDUCE / BUY_TO_CLOSE / SELL_TO_ADD_SHORT as
    its legal moves. This is what the brain reads to know 'BUY does
    not mean open long here'."""
    t = allowed_transitions_for("short")
    assert "BUY_TO_REDUCE" in t
    assert "BUY_TO_CLOSE" in t
    assert "SELL_TO_ADD_SHORT" in t


def test_allowed_transitions_long_includes_sell_to_close():
    t = allowed_transitions_for("long")
    assert "SELL_TO_REDUCE" in t
    assert "SELL_TO_CLOSE" in t
    assert "BUY_TO_ADD_LONG" in t


def test_allowed_transitions_flat_only_opens():
    t = allowed_transitions_for("flat")
    assert t == ["BUY_TO_OPEN_LONG", "SELL_TO_OPEN_SHORT"]


# ── brain_core integration: position_context drives transition layer ──


def test_brain_core_evaluate_with_short_position_makes_buy_a_close():
    """End-to-end: when the brain receives a SHORT position_context
    and ends up emitting BUY (e.g. signal strong enough), the
    BrainIntent must stamp transition_intent=CLOSE and
    target_exposure=FLAT — NOT OPEN/LONG. This is the AAPL fix."""
    sys.path.insert(0, "/app/external")
    from brains.brain_core import NeutralAdversarialBrain

    brain = NeutralAdversarialBrain(
        brain_id="alpha", display_name="Camino",
        lane="equity", shadow_only=True, min_commitment=0.0, min_gap=0.0,
    )
    snapshot = {
        "symbol": "AAPL", "price": 195.0, "price_change_pct": 2.0,
        "volume_change_pct": 50.0, "rsi": 38.0, "spread_bps": 3.0,
        "volatility": 0.2, "trend_score": 0.6, "liquidity_score": 0.9,
        "market_regime": "calm", "setup_score": 0.4,
    }
    ctx = {
        "symbol": "AAPL", "lane": "equity",
        "current_side": "SHORT", "signed_qty": -100,
        "allowed_transitions": ["BUY_TO_REDUCE", "BUY_TO_CLOSE", "SELL_TO_ADD_SHORT"],
    }
    intent = brain.evaluate("AAPL", snapshot, position_context=ctx)
    assert intent.current_side == "SHORT"
    assert intent.signed_qty == -100
    # When the resolved action is BUY against a SHORT, transition
    # must be CLOSE / target FLAT — NEVER OPEN_LONG.
    if intent.order_action == "BUY":
        assert intent.transition_intent == "CLOSE"
        assert intent.target_exposure == "FLAT"
    elif intent.order_action == "SELL":
        assert intent.transition_intent == "ADD"
        assert intent.target_exposure == "SHORT"


def test_brain_core_evaluate_with_flat_context_falls_back_to_open():
    sys.path.insert(0, "/app/external")
    from brains.brain_core import NeutralAdversarialBrain

    brain = NeutralAdversarialBrain(
        brain_id="alpha", display_name="Camino",
        lane="equity", shadow_only=True, min_commitment=0.0, min_gap=0.0,
    )
    snapshot = {
        "symbol": "AAPL", "price": 195.0, "price_change_pct": 2.0,
        "volume_change_pct": 50.0, "rsi": 38.0, "spread_bps": 3.0,
        "volatility": 0.2, "trend_score": 0.6, "liquidity_score": 0.9,
        "market_regime": "calm", "setup_score": 0.0,
    }
    intent = brain.evaluate("AAPL", snapshot)  # no context = legacy
    assert intent.current_side == "FLAT"
    if intent.order_action == "BUY":
        assert intent.transition_intent == "OPEN"
        assert intent.target_exposure == "LONG"
    elif intent.order_action == "SELL":
        assert intent.transition_intent == "OPEN"
        assert intent.target_exposure == "SHORT"
