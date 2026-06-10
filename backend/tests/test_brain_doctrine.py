"""Tests for the brain doctrine + seat layer.

Doctrine pin (operator directive, 2026-06-XX):
    brain_id  = who it is        (Camino, Barracuda, Hellcat, GTO)
    doctrine  = how it thinks    (bound to brain_id, immutable)
    seat      = what job today    (runtime-rotatable)

These tests lock the architectural rule: doctrine MUST follow
brain_id, NOT seat. If a brain rotates from strategist to executor,
its doctrine MUST be unchanged.
"""
import sys

sys.path.insert(0, "/app/backend")
sys.path.insert(0, "/app/external")

import pytest  # noqa: E402

from shared.brain_doctrine import (  # noqa: E402
    BRAIN_ID_TO_STACK,
    DOCTRINES,
    STACK_TO_BRAIN_ID,
    get_doctrine,
)
from brains.brain_core import NeutralAdversarialBrain  # noqa: E402


# ── Doctrine bound to brain_id, not seat ──────────────────────────


def test_each_brain_has_a_distinct_doctrine():
    """The whole point of the layer: four brains, four interpretations.
    If two brains share a doctrine, the adversarial layer is fake."""
    doctrines = {b.doctrine for b in DOCTRINES.values()}
    assert doctrines == {"trend", "mean_reversion", "breakout", "momentum"}


def test_camino_is_trend():
    d = get_doctrine("camino")
    assert d.doctrine == "trend"
    assert d.trend_weight > d.mean_reversion_weight
    assert d.trend_weight > d.breakout_weight


def test_barracuda_is_mean_reversion():
    d = get_doctrine("barracuda")
    assert d.doctrine == "mean_reversion"
    assert d.mean_reversion_weight > d.trend_weight
    assert d.mean_reversion_weight > d.breakout_weight


def test_hellcat_is_breakout():
    d = get_doctrine("hellcat")
    assert d.doctrine == "breakout"
    assert d.breakout_weight > d.trend_weight
    assert d.breakout_weight > d.mean_reversion_weight


def test_gto_is_momentum():
    d = get_doctrine("gto")
    assert d.doctrine == "momentum"
    assert d.momentum_weight > d.breakout_weight
    assert d.momentum_weight > d.mean_reversion_weight


def test_legacy_stack_codes_map_to_canonical_brain_ids():
    """Doctrine lookup must accept both vocabularies during transition."""
    assert get_doctrine("alpha").doctrine == "trend"        # alpha = camino
    assert get_doctrine("camaro").doctrine == "mean_reversion"  # = barracuda
    assert get_doctrine("chevelle").doctrine == "breakout"   # = hellcat
    assert get_doctrine("redeye").doctrine == "momentum"     # = gto


def test_stack_brainid_maps_are_inverses():
    for stack, bid in STACK_TO_BRAIN_ID.items():
        assert BRAIN_ID_TO_STACK[bid] == stack


def test_unknown_brain_id_raises():
    with pytest.raises(ValueError):
        get_doctrine("nonexistent")


# ── Doctrine actually differentiates brain output ─────────────────


def _snapshot_overbought_uptrend():
    """A snapshot that's bullish on momentum/trend but stretched on
    mean-reversion (RSI=80). Different doctrines should disagree."""
    return {
        "symbol": "AAPL", "price": 195.0, "price_change_pct": 4.0,
        "volume_change_pct": 80.0, "rsi": 80.0, "spread_bps": 2.0,
        "volatility": 0.15, "trend_score": 0.85, "liquidity_score": 0.95,
        "market_regime": "calm", "setup_score": 0.7,
    }


def _build_brain(brain_id: str):
    return NeutralAdversarialBrain(
        brain_id=brain_id,
        display_name=get_doctrine(brain_id).display_name,
        lane="equity", shadow_only=True,
        max_shadow_size=0.0,
        doctrine=get_doctrine(brain_id),
    )


def test_camino_and_barracuda_disagree_on_overbought_uptrend():
    """The AAPL-incident lesson made visible: when a stretched uptrend
    is in front of them, Camino (trend) should lean BUY, Barracuda
    (mean_reversion) should lean either SELL or HOLD. They are NOT
    the same algorithm anymore."""
    snap = _snapshot_overbought_uptrend()
    camino = _build_brain("camino").evaluate("AAPL", snap)
    barracuda = _build_brain("barracuda").evaluate("AAPL", snap)
    # The hypothesis_buy score for the trend brain should be
    # higher than for the mean-reversion brain on this snapshot.
    assert (
        camino.hypothesis_scores["hypothesis_buy"]
        > barracuda.hypothesis_scores["hypothesis_buy"]
    ), (
        f"Camino BUY={camino.hypothesis_scores['hypothesis_buy']:.3f} "
        f"should exceed Barracuda BUY="
        f"{barracuda.hypothesis_scores['hypothesis_buy']:.3f} "
        "on a stretched uptrend"
    )
    # And conversely Barracuda's SELL should outscore Camino's SELL
    # (mean rev wants to fade an RSI=80 print).
    assert (
        barracuda.hypothesis_scores["hypothesis_sell"]
        > camino.hypothesis_scores["hypothesis_sell"]
    )


def test_hellcat_loves_a_high_setup_score():
    """Breakout doctrine should weight setup_score most heavily —
    the same snapshot with high setup_score should produce a higher
    BUY score for Hellcat than for any other brain."""
    snap = {
        "symbol": "NVDA", "price": 800.0, "price_change_pct": 0.5,
        "volume_change_pct": 40.0, "rsi": 55.0, "spread_bps": 1.5,
        "volatility": 0.1, "trend_score": 0.2, "liquidity_score": 0.9,
        "market_regime": "calm", "setup_score": 0.8,
    }
    hellcat = _build_brain("hellcat").evaluate("NVDA", snap)
    camino = _build_brain("camino").evaluate("NVDA", snap)
    barracuda = _build_brain("barracuda").evaluate("NVDA", snap)
    gto = _build_brain("gto").evaluate("NVDA", snap)
    h_buy = hellcat.hypothesis_scores["hypothesis_buy"]
    # Hellcat's BUY must beat ALL others on a breakout-dominant snapshot.
    others = {
        "camino": camino.hypothesis_scores["hypothesis_buy"],
        "barracuda": barracuda.hypothesis_scores["hypothesis_buy"],
        "gto": gto.hypothesis_scores["hypothesis_buy"],
    }
    assert all(h_buy > v for v in others.values()), (
        f"Hellcat BUY={h_buy:.3f} should beat others: {others}"
    )


def test_min_confidence_and_min_gap_come_from_doctrine():
    """Doctrine's thresholds must override the constructor defaults."""
    cam = NeutralAdversarialBrain(
        brain_id="camino", display_name="Camino",
        lane="equity", shadow_only=True,
        min_commitment=0.99, min_gap=0.99,   # nonsense values
        doctrine=get_doctrine("camino"),
    )
    assert cam.min_commitment == pytest.approx(0.62)
    assert cam.min_gap == pytest.approx(0.08)


# ── Doctrine stamped on intent ────────────────────────────────────


def test_intent_carries_doctrine_name():
    snap = _snapshot_overbought_uptrend()
    intent = _build_brain("camino").evaluate("AAPL", snap)
    assert intent.doctrine == "trend"


def test_legacy_brain_without_doctrine_emits_none_doctrine():
    """Backward-compat: a brain constructed without a doctrine still
    works, and emits doctrine=None — the legacy weights run."""
    legacy = NeutralAdversarialBrain(
        brain_id="alpha", display_name="Camino",
        lane="equity", shadow_only=True,
        min_commitment=0.58, min_gap=0.06,
    )
    intent = legacy.evaluate("AAPL", _snapshot_overbought_uptrend())
    assert intent.doctrine is None


# ── Seat: orthogonal to brain_id and doctrine ─────────────────────


def test_seat_is_stamped_on_intent_when_provided():
    snap = _snapshot_overbought_uptrend()
    intent = _build_brain("camino").evaluate("AAPL", snap, seat="executor")
    assert intent.seat == "executor"
    # Doctrine MUST be unchanged by seat.
    assert intent.doctrine == "trend"


def test_seat_change_does_not_change_doctrine():
    """The core architectural rule: rotating seat MUST NOT change how
    the brain thinks. Same brain, same snapshot, two seats → same
    doctrine, same hypothesis scores."""
    brain = _build_brain("camino")
    snap = _snapshot_overbought_uptrend()
    as_strategist = brain.evaluate("AAPL", snap, seat="strategist")
    as_auditor = brain.evaluate("AAPL", snap, seat="auditor")
    # Different seats stamped...
    assert as_strategist.seat == "strategist"
    assert as_auditor.seat == "auditor"
    # ...but doctrine and scores are identical.
    assert as_strategist.doctrine == as_auditor.doctrine == "trend"
    assert (
        as_strategist.hypothesis_scores["hypothesis_buy"]
        == as_auditor.hypothesis_scores["hypothesis_buy"]
    )


def test_seat_optional_intent_seat_is_none_when_not_provided():
    intent = _build_brain("camino").evaluate("AAPL", _snapshot_overbought_uptrend())
    assert intent.seat is None
