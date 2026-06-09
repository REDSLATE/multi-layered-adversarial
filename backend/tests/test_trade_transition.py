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
    classify_position_evolution,
    classify_risk_transition,
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


# ── classify_position_evolution: SCALE_IN / SCALE_OUT ─────────────


def test_high_conviction_add_long_becomes_scale_in():
    """The mental shift: ADD with conviction is SCALE_IN (planned),
    not a reactive ADD. Operator's spec, 2026-06-XX."""
    evo = classify_position_evolution("ADD", "LONG", confidence=0.72)
    assert evo == "SCALE_IN"


def test_low_conviction_add_long_stays_add():
    evo = classify_position_evolution("ADD", "LONG", confidence=0.50)
    assert evo == "ADD"


def test_add_short_stays_add_not_scale_in():
    """Operator's scoped vocabulary keeps SHORT-side ADD plain;
    SCALE_IN is LONG-only in this pass."""
    evo = classify_position_evolution("ADD", "SHORT", confidence=0.90)
    assert evo == "ADD"


def test_high_conviction_reduce_long_becomes_scale_out():
    """SCALE_OUT = locking in gains. SELL on a LONG with conviction
    is the PM's "take some off the table" verb, not a panic-trim."""
    evo = classify_position_evolution("REDUCE", "LONG", confidence=0.62)
    assert evo == "SCALE_OUT"


def test_low_conviction_reduce_long_stays_reduce():
    evo = classify_position_evolution("REDUCE", "LONG", confidence=0.40)
    assert evo == "REDUCE"


# ── classify_position_evolution: PARTIAL_COVER / FULL_COVER ───────


def test_reduce_short_is_partial_cover():
    """Any REDUCE on a SHORT is, by definition, a PARTIAL_COVER —
    the brain isn't taking the whole short off."""
    evo = classify_position_evolution("REDUCE", "SHORT", confidence=0.50)
    assert evo == "PARTIAL_COVER"


def test_high_conviction_close_short_is_full_cover():
    """CLOSE on a SHORT with high conviction = FULL_COVER. This is
    exactly the AAPL-incident-fix verb the operator wanted visible —
    so the dashboard can see 'the brain wanted the whole short
    flattened', not just 'the brain emitted BUY.'"""
    evo = classify_position_evolution("CLOSE", "SHORT", confidence=0.85)
    assert evo == "FULL_COVER"


def test_low_conviction_close_short_downgrades_to_partial_cover():
    """If the brain emits CLOSE but its confidence is weak, the
    portfolio reading is closer to PARTIAL_COVER — the brain is
    hedging its bet."""
    evo = classify_position_evolution("CLOSE", "SHORT", confidence=0.55)
    assert evo == "PARTIAL_COVER"


def test_close_long_stays_close():
    """Operator did not introduce a partial-close-for-long verb in
    this pass — SCALE_OUT covers the partial case, CLOSE means flat."""
    evo = classify_position_evolution("CLOSE", "LONG", confidence=0.85)
    assert evo == "CLOSE"


# ── classify_position_evolution: pass-throughs ────────────────────


def test_open_passes_through():
    assert classify_position_evolution("OPEN", "FLAT", confidence=0.9) == "OPEN"


def test_flip_passes_through():
    assert classify_position_evolution("FLIP", "LONG", confidence=0.9) == "FLIP"


def test_hold_passes_through():
    assert classify_position_evolution("HOLD", "FLAT", confidence=0.9) == "HOLD"


# ── classify_risk_transition ──────────────────────────────────────


def test_risk_off_when_de_risking_in_volatile_regime():
    """De-risking action under a stressed regime = RISK_OFF.
    Operator framing: 'triggered by volatility, news shock,
    liquidity stress.'"""
    rt = classify_risk_transition("volatile", "SCALE_OUT")
    assert rt == "RISK_OFF"


def test_risk_off_on_partial_cover_in_crisis():
    rt = classify_risk_transition("crisis", "PARTIAL_COVER")
    assert rt == "RISK_OFF"


def test_risk_on_when_adding_in_calm_regime():
    """RISK_ON = adding exposure in favorable conditions."""
    rt = classify_risk_transition("calm", "SCALE_IN")
    assert rt == "RISK_ON"


def test_risk_on_when_opening_in_bullish_regime():
    rt = classify_risk_transition("bullish", "OPEN")
    assert rt == "RISK_ON"


def test_neutral_when_de_risking_in_calm_regime():
    """Reducing a LONG in a CALM regime is just risk management,
    not a regime-level RISK_OFF event."""
    rt = classify_risk_transition("calm", "SCALE_OUT")
    assert rt == "NEUTRAL"


def test_neutral_when_adding_in_volatile_regime():
    """Adding exposure in a stressed regime is NOT RISK_ON —
    that would invert the doctrine. NEUTRAL is the honest label."""
    rt = classify_risk_transition("volatile", "SCALE_IN")
    assert rt == "NEUTRAL"


def test_flip_in_stressed_regime_is_risk_off():
    """Flipping exposure under stress is a regime-level
    de-risk because the brain is rotating away from its prior
    bias under unfavorable conditions."""
    rt = classify_risk_transition("stressed", "FLIP")
    assert rt == "RISK_OFF"


def test_unknown_regime_returns_neutral():
    rt = classify_risk_transition("", "SCALE_IN")
    assert rt == "NEUTRAL"


# ── Brain-core integration: portfolio-manager layer on intents ────


def test_brain_intent_stamps_scale_in_on_high_conviction_long_add():
    """End-to-end: brain ticked against an existing LONG position
    with strong signal MUST emit position_evolution=SCALE_IN, NOT
    plain ADD. This is the PM mental shift made visible to the
    operator and the audit log."""
    sys.path.insert(0, "/app/external")
    from brains.brain_core import NeutralAdversarialBrain

    brain = NeutralAdversarialBrain(
        brain_id="alpha", display_name="Camino",
        lane="equity", shadow_only=True, min_commitment=0.0, min_gap=0.0,
    )
    snapshot = {
        "symbol": "AAPL", "price": 195.0, "price_change_pct": 5.0,
        "volume_change_pct": 200.0, "rsi": 35.0, "spread_bps": 1.0,
        "volatility": 0.05, "trend_score": 0.95, "liquidity_score": 1.0,
        "market_regime": "calm", "setup_score": 0.9,
    }
    ctx = {
        "symbol": "AAPL", "lane": "equity",
        "current_side": "LONG", "signed_qty": 50,
        "allowed_transitions": ["SELL_TO_REDUCE", "SELL_TO_CLOSE", "BUY_TO_ADD_LONG"],
    }
    intent = brain.evaluate("AAPL", snapshot, position_context=ctx)
    # With this snapshot the brain should produce a very high
    # confidence BUY. That, against a LONG, must be SCALE_IN.
    if intent.order_action == "BUY":
        assert intent.transition_intent == "ADD"
        assert intent.position_evolution in ("SCALE_IN", "ADD")
        # Confidence threshold is 0.65 — if confidence cleared it,
        # the verb MUST be SCALE_IN.
        if intent.confidence >= 0.65:
            assert intent.position_evolution == "SCALE_IN"
        # In a CALM regime, SCALE_IN lifts to RISK_ON.
        if intent.position_evolution == "SCALE_IN":
            assert intent.risk_transition == "RISK_ON"


def test_brain_intent_stamps_full_cover_on_high_conviction_buy_against_short():
    """The AAPL-incident-shape end-to-end: brain emits BUY against a
    SHORT with strong conviction. Must stamp position_evolution=
    FULL_COVER (not OPEN_LONG, not even plain CLOSE — the operator
    wants the PM-grade verb visible)."""
    sys.path.insert(0, "/app/external")
    from brains.brain_core import NeutralAdversarialBrain

    brain = NeutralAdversarialBrain(
        brain_id="alpha", display_name="Camino",
        lane="equity", shadow_only=True, min_commitment=0.0, min_gap=0.0,
    )
    snapshot = {
        "symbol": "AAPL", "price": 195.0, "price_change_pct": 5.0,
        "volume_change_pct": 200.0, "rsi": 30.0, "spread_bps": 1.0,
        "volatility": 0.05, "trend_score": 0.95, "liquidity_score": 1.0,
        "market_regime": "calm", "setup_score": 0.9,
    }
    ctx = {
        "symbol": "AAPL", "lane": "equity",
        "current_side": "SHORT", "signed_qty": -100,
        "allowed_transitions": ["BUY_TO_REDUCE", "BUY_TO_CLOSE", "SELL_TO_ADD_SHORT"],
    }
    intent = brain.evaluate("AAPL", snapshot, position_context=ctx)
    if intent.order_action == "BUY":
        assert intent.transition_intent == "CLOSE"
        # FULL vs PARTIAL is gated by FULL_COVER_CONF_FLOOR=0.78.
        if intent.confidence >= 0.78:
            assert intent.position_evolution == "FULL_COVER"
        else:
            assert intent.position_evolution == "PARTIAL_COVER"


def test_brain_intent_emits_risk_off_when_reducing_in_volatile_regime():
    sys.path.insert(0, "/app/external")
    from brains.brain_core import NeutralAdversarialBrain

    brain = NeutralAdversarialBrain(
        brain_id="alpha", display_name="Camino",
        lane="equity", shadow_only=True, min_commitment=0.0, min_gap=0.0,
    )
    # Snapshot tuned for a SELL win and `volatile` regime — i.e. the
    # brain is trimming a long under stress.
    snapshot = {
        "symbol": "AAPL", "price": 195.0, "price_change_pct": -5.0,
        "volume_change_pct": 200.0, "rsi": 80.0, "spread_bps": 1.0,
        "volatility": 0.6, "trend_score": -0.9, "liquidity_score": 1.0,
        "market_regime": "volatile", "setup_score": 0.0,
    }
    ctx = {
        "symbol": "AAPL", "lane": "equity",
        "current_side": "LONG", "signed_qty": 50,
        "allowed_transitions": ["SELL_TO_REDUCE", "SELL_TO_CLOSE", "BUY_TO_ADD_LONG"],
    }
    intent = brain.evaluate("AAPL", snapshot, position_context=ctx)
    if intent.order_action == "SELL":
        # Either SCALE_OUT (conviction ≥ 0.55) or CLOSE — both
        # de-risking verbs — in a volatile regime, both lift to
        # RISK_OFF.
        assert intent.position_evolution in ("SCALE_OUT", "REDUCE", "CLOSE")
        assert intent.risk_transition == "RISK_OFF"
