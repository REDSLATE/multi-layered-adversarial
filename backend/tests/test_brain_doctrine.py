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
    # 2026-02-21: STACK_TO_BRAIN_ID is many-to-one (alpha + camino both
    # map to "camino"), so the dict-inverse cannot round-trip. What we
    # actually need to guarantee is:
    #   (a) every canonical brain_id appears as a key in BRAIN_ID_TO_STACK
    #   (b) every value in BRAIN_ID_TO_STACK is itself a key in
    #       STACK_TO_BRAIN_ID (so chains terminate)
    canonical = {"camino", "barracuda", "hellcat", "gto"}
    for bid in canonical:
        assert bid in BRAIN_ID_TO_STACK
        assert BRAIN_ID_TO_STACK[bid] in STACK_TO_BRAIN_ID
        assert STACK_TO_BRAIN_ID[BRAIN_ID_TO_STACK[bid]] == bid


def test_unknown_brain_id_raises():
    with pytest.raises(ValueError):
        get_doctrine("nonexistent")



# ── Note (2026-02-11 iter-28j) ────────────────────────────────────
# The pre-P7 tests below this line exercised `brains.brain_core.
# NeutralAdversarialBrain.evaluate(...)` — the legacy core deleted
# in the P7 strategy split. New behaviour lives in
# `mc_brains/strategies/{trend_following,momentum_confirmation,
# mean_reversion,execution_safety}.py` and its distinctness is
# guarded by `mc_pulse/tests/test_p7c_personality_separation.py`
# and the Pulse Health `mc_pulse_health_snapshots` distinctness
# metrics. No point re-instrumenting the deleted core here.
