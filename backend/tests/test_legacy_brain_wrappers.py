"""Tests for the legacy brain wrapper layer.

Doctrine pin (operator directive, 2026-06-XX):

    new brain engine
        + old Alpha executor instincts on Camino
        + old Chevelle governor instincts on Hellcat
    without locking either one into a seat

Assignment:
    Camino    → alpha_legacy_executor
    Hellcat   → chevelle_legacy_governor
    Barracuda → no wrapper (pure mean-reversion)
    GTO       → no wrapper (pure momentum/adversary)

A wrapper MUST NEVER:
    - flip action (BUY ↔ SELL)
    - create a trade from HOLD
    - force a seat

These tests pin those invariants.
"""
import sys

sys.path.insert(0, "/app/backend")

from shared.legacy_brain_wrappers import (  # noqa: E402
    BRAIN_WRAPPER_ASSIGNMENTS,
    apply_alpha_legacy_executor,
    apply_chevelle_legacy_governor,
    apply_legacy_wrapper,
    clamp,
    safe_float,
)


# ── Wrapper assignment registry ───────────────────────────────────


def test_camino_assigned_alpha_wrapper():
    assert BRAIN_WRAPPER_ASSIGNMENTS["camino"] == "alpha_legacy_executor"


def test_hellcat_assigned_chevelle_wrapper():
    assert BRAIN_WRAPPER_ASSIGNMENTS["hellcat"] == "chevelle_legacy_governor"


def test_barracuda_has_no_wrapper():
    assert "barracuda" not in BRAIN_WRAPPER_ASSIGNMENTS


def test_gto_has_no_wrapper():
    assert "gto" not in BRAIN_WRAPPER_ASSIGNMENTS


def test_apply_legacy_wrapper_passthrough_for_unassigned():
    """Brains without a wrapper assignment must pass through unchanged."""
    inp = {"brain_id": "barracuda", "action": "BUY", "confidence": 0.71}
    out = apply_legacy_wrapper(inp)
    assert out is inp  # literally the same dict


# ── Invariants the wrapper MUST honor ─────────────────────────────


def _base_intent(**overrides):
    base = {
        "brain_id": "camino",
        "display_name": "Camino",
        "action": "BUY",
        "confidence": 0.70,
        "size_bias": 1.0,
        "current_side": "LONG",
        "transition_intent": "ADD_LONG",
        "position_evolution": "ADD",
        "risk_transition": "NEUTRAL",
        "reasons": [],
        "warnings": [],
        "evidence": {},
    }
    base.update(overrides)
    return base


def test_alpha_wrapper_never_flips_action():
    out = apply_alpha_legacy_executor(_base_intent(action="BUY"))
    assert out["action"] == "BUY"
    out = apply_alpha_legacy_executor(_base_intent(action="SELL"))
    assert out["action"] == "SELL"


def test_chevelle_wrapper_never_flips_action():
    out = apply_chevelle_legacy_governor(_base_intent(action="BUY", brain_id="hellcat"))
    assert out["action"] == "BUY"
    out = apply_chevelle_legacy_governor(_base_intent(action="SELL", brain_id="hellcat"))
    assert out["action"] == "SELL"


def test_chevelle_wrapper_zeros_size_on_hold():
    out = apply_chevelle_legacy_governor(_base_intent(
        action="HOLD", brain_id="hellcat",
    ))
    assert out["action"] == "HOLD"
    assert out["size_bias"] == 0.0


def test_neither_wrapper_creates_a_trade_from_hold():
    """The wrapper must not promote HOLD to BUY/SELL."""
    a = apply_alpha_legacy_executor(_base_intent(action="HOLD"))
    c = apply_chevelle_legacy_governor(_base_intent(action="HOLD", brain_id="hellcat"))
    assert a["action"] == "HOLD"
    assert c["action"] == "HOLD"


# ── Alpha executor behavior ───────────────────────────────────────


def test_alpha_rewards_strong_open_long_commitment():
    """Clean ADD_LONG + confidence >= 0.68 → confidence lifted,
    size_bias boosted, reason logged."""
    out = apply_alpha_legacy_executor(_base_intent(
        confidence=0.70, transition_intent="ADD_LONG",
    ))
    assert out["confidence"] > 0.70
    assert out["size_bias"] > 1.0
    assert any("CLEAN_EXECUTION_COMMITMENT" in r for r in out["reasons"])


def test_alpha_penalizes_weak_open_commitment():
    out = apply_alpha_legacy_executor(_base_intent(
        confidence=0.55, transition_intent="OPEN_LONG",
    ))
    assert out["confidence"] < 0.55
    assert out["size_bias"] < 1.0
    assert any("WEAK_COMMITMENT_FOR_EXPOSURE_INCREASE" in w for w in out["warnings"])


def test_alpha_penalizes_unknown_position_state():
    """The AAPL-incident lesson: if current_side is unknown, Alpha
    instinct says compress and warn — don't act blind."""
    out = apply_alpha_legacy_executor(_base_intent(current_side=None))
    assert out["confidence"] < 0.70
    assert out["size_bias"] < 1.0
    assert any("POSITION_STATE_UNKNOWN" in w for w in out["warnings"])


def test_alpha_rewards_confirmed_scale_in():
    out = apply_alpha_legacy_executor(_base_intent(
        confidence=0.75, position_evolution="SCALE_IN",
    ))
    assert any("SCALE_IN_CONFIRMED" in r for r in out["reasons"])


def test_alpha_warns_on_unconfirmed_scale_in():
    out = apply_alpha_legacy_executor(_base_intent(
        confidence=0.65, position_evolution="SCALE_IN",
    ))
    assert any("SCALE_IN_NOT_CONFIRMED" in w for w in out["warnings"])


def test_alpha_compresses_flip_heavily():
    out = apply_alpha_legacy_executor(_base_intent(
        transition_intent="FLIP_LONG_TO_SHORT",
    ))
    assert out["size_bias"] <= 0.50
    assert any("FLIP_REQUIRES_STRONG_CONFIRMATION" in w for w in out["warnings"])


def test_alpha_stamps_provenance_block():
    out = apply_alpha_legacy_executor(_base_intent())
    lw = out["evidence"]["legacy_wrapper"]
    assert lw["name"] == "alpha_legacy_executor"
    assert lw["parent_brain"] == "alpha"
    assert out["wrapper"] == "alpha_legacy_executor"
    assert out["parent_brain"] == "alpha"


# ── Chevelle governor behavior ────────────────────────────────────


def test_chevelle_compresses_exposure_in_risk_off():
    """RISK_OFF + ADD_LONG → confidence dropped, size halved, warn."""
    out = apply_chevelle_legacy_governor(_base_intent(
        brain_id="hellcat", risk_transition="RISK_OFF",
        transition_intent="ADD_LONG",
    ))
    assert out["confidence"] < 0.70
    assert out["size_bias"] <= 0.50
    assert any("RISK_OFF_EXPOSURE_INCREASE_COMPRESSED" in w for w in out["warnings"])


def test_chevelle_approves_reductions_in_risk_off():
    out = apply_chevelle_legacy_governor(_base_intent(
        brain_id="hellcat", risk_transition="RISK_OFF",
        position_evolution="SCALE_OUT", transition_intent="REDUCE_LONG",
    ))
    assert out["confidence"] > 0.70
    assert any("RISK_OFF_REDUCTION_APPROVED" in r for r in out["reasons"])


def test_chevelle_allows_exposure_in_risk_on():
    out = apply_chevelle_legacy_governor(_base_intent(
        brain_id="hellcat", risk_transition="RISK_ON",
        transition_intent="OPEN_LONG",
    ))
    assert out["confidence"] >= 0.70
    assert any("RISK_ON_EXPOSURE_ALLOWED" in r for r in out["reasons"])


def test_chevelle_compresses_scale_in_size():
    """Hellcat-as-governor instinct: SCALE_IN gets size compressed
    even when conditions allow it. Risk discipline first."""
    out = apply_chevelle_legacy_governor(_base_intent(
        brain_id="hellcat", position_evolution="SCALE_IN",
    ))
    assert out["size_bias"] < 1.0
    assert any("SCALE_IN_SIZE_COMPRESSION" in w for w in out["warnings"])


def test_chevelle_heavily_compresses_flip():
    out = apply_chevelle_legacy_governor(_base_intent(
        brain_id="hellcat", transition_intent="FLIP_SHORT_TO_LONG",
    ))
    assert out["size_bias"] <= 0.35
    assert any("FLIP_HEAVILY_COMPRESSED" in w for w in out["warnings"])


def test_chevelle_penalizes_unknown_position_state():
    out = apply_chevelle_legacy_governor(_base_intent(
        brain_id="hellcat", current_side=None,
    ))
    assert out["confidence"] < 0.70
    assert any("POSITION_STATE_UNKNOWN" in w for w in out["warnings"])


# ── Confidence + size_bias always within bounds ───────────────────


def test_confidence_clamped_to_0_1():
    """Even with extreme inputs, confidence must stay in [0,1]."""
    out = apply_alpha_legacy_executor(_base_intent(confidence=2.5))
    assert 0.0 <= out["confidence"] <= 1.0
    out = apply_alpha_legacy_executor(_base_intent(confidence=-0.5))
    assert 0.0 <= out["confidence"] <= 1.0


def test_size_bias_clamped_to_0_2():
    """size_bias must stay in [0, 2.0]."""
    out = apply_chevelle_legacy_governor(_base_intent(
        brain_id="hellcat", size_bias=99.0,
    ))
    assert 0.0 <= out["size_bias"] <= 2.0


# ── Public helpers ────────────────────────────────────────────────


def test_clamp_helper():
    assert clamp(0.5) == 0.5
    assert clamp(1.5) == 1.0
    assert clamp(-1.0) == 0.0
    assert clamp(0.5, 0.0, 0.4) == 0.4


def test_safe_float_handles_garbage():
    assert safe_float("3.5") == 3.5
    assert safe_float(None, 0.7) == 0.7
    assert safe_float("abc", 0.5) == 0.5
    assert safe_float({}, -1.0) == -1.0
