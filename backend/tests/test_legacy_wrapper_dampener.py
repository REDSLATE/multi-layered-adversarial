"""Tests for the legacy-wrapper penalty-stacking dampener (P1, 2026-02-19).

Operator directive: the 4 wrappers in `legacy_brain_wrappers.py`
multiply `size_bias` 6-9 times each. A realistic BUY on AAPL in chop
regime with unknown position can compound 4-6 penalty factors to
~0.18x — functionally muting the intent. Two env knobs let the
operator dial penalty strength without code change:

  RISEDUAL_WRAPPER_PENALTY_STRENGTH   1.0=current 0.0=disable wrapper
  RISEDUAL_WRAPPER_MIN_SIZE_BIAS_NONZERO   directional-only floor

These tests pin:
  * Default (strength=1.0, floor=0.0) preserves current behavior.
  * Strength=0.0 fully neutralizes the wrapper (size + confidence
    revert to base).
  * Strength=0.5 cuts deviation in half (linear interpolation).
  * Floor=0.3 clamps stacked penalty results UP for BUY/SELL only.
  * HOLD never gets the floor.
  * Diagnostics block stamps into evidence.legacy_wrapper.dampener
    only when something actually changed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from shared.legacy_brain_wrappers import (  # noqa: E402
    apply_alpha_legacy_executor,
    apply_camaro_legacy_strategist,
    apply_chevelle_legacy_governor,
    apply_redeye_legacy_adversary,
    _finalise_size_and_confidence,
)


# ── helpers ───────────────────────────────────────────────────────


def _heavy_penalty_buy_intent() -> dict:
    """Build a BUY intent that will trip MANY of the wrappers'
    multiplicative penalties — the worst case the dampener exists
    for. Specifically chosen to stack:
      * unknown position state          (alpha + chevelle: ×0.70/0.60)
      * weak commitment threshold       (alpha: ×0.85)
      * chop regime                     (camaro: ×0.80)
      * tiny score gap                  (camaro: ×0.75)
      * weak consensus                  (redeye: ×0.70)
      * bearish flow                    (redeye: ×0.65)
    """
    return {
        "brain_id": "camino",
        "display_name": "Camino",
        "action": "BUY",
        "confidence": 0.55,
        "size_bias": 1.0,
        "current_side": "UNKNOWN",
        "transition_intent": "OPEN_LONG",
        "position_evolution": None,
        "risk_transition": None,
        "evidence": {
            "market_regime": "chop",
            "buy_score": 0.50,
            "sell_score": 0.49,    # 0.01 gap → tiny score gap penalty
            "flow_imbalance": -0.30,
        },
    }


# ── unit tests on the finalizer directly ──────────────────────────


def test_finalizer_default_passes_through(monkeypatch):
    """Default config (strength=1.0, floor=0.0) must NOT change the
    wrapper's accumulated values."""
    monkeypatch.delenv("RISEDUAL_WRAPPER_PENALTY_STRENGTH", raising=False)
    monkeypatch.delenv("RISEDUAL_WRAPPER_MIN_SIZE_BIAS_NONZERO", raising=False)
    sb, conf, damp = _finalise_size_and_confidence(
        final_size_bias=0.18, final_confidence=0.30,
        base_size_bias=1.0, base_confidence=0.55,
        action="BUY",
    )
    assert sb == pytest.approx(0.18)
    assert conf == pytest.approx(0.30)
    # Default config writes only the knob values into diagnostics —
    # no "pre_dampener_*" keys because the dampener didn't actually
    # change anything.
    assert damp == {"penalty_strength": 1.0, "min_size_bias_nonzero": 0.0}


def test_finalizer_strength_zero_neutralizes_wrapper(monkeypatch):
    """At strength=0.0 the dampener must fully neutralize the wrapper:
    size_bias and confidence revert to the base inputs."""
    monkeypatch.setenv("RISEDUAL_WRAPPER_PENALTY_STRENGTH", "0.0")
    sb, conf, damp = _finalise_size_and_confidence(
        final_size_bias=0.18, final_confidence=0.30,
        base_size_bias=1.0, base_confidence=0.55,
        action="BUY",
    )
    assert sb == pytest.approx(1.0), "strength=0 must revert to base"
    assert conf == pytest.approx(0.55)
    assert damp["pre_dampener_size_bias"] == pytest.approx(0.18)


def test_finalizer_strength_half_linearly_interpolates(monkeypatch):
    """At strength=0.5 the wrapper's deviation from base is cut in
    half — linear interpolation between base and wrapper output."""
    monkeypatch.setenv("RISEDUAL_WRAPPER_PENALTY_STRENGTH", "0.5")
    sb, conf, _ = _finalise_size_and_confidence(
        final_size_bias=0.18, final_confidence=0.30,
        base_size_bias=1.0, base_confidence=0.55,
        action="BUY",
    )
    # 1.0 + (0.18 - 1.0) * 0.5 = 1.0 + (-0.41) = 0.59
    assert sb == pytest.approx(0.59)
    # 0.55 + (0.30 - 0.55) * 0.5 = 0.55 + (-0.125) = 0.425
    assert conf == pytest.approx(0.425)


def test_finalizer_floor_only_applies_to_directional(monkeypatch):
    """The directional floor clamps BUY/SELL size_bias UP. HOLD never
    gets the floor — HOLD size_bias passes through (or stays 0 if
    the wrapper already zeroed it)."""
    monkeypatch.setenv("RISEDUAL_WRAPPER_MIN_SIZE_BIAS_NONZERO", "0.3")
    # BUY case: 0.18 < 0.3 → floored up to 0.3.
    sb_buy, _, damp_buy = _finalise_size_and_confidence(
        final_size_bias=0.18, final_confidence=0.30,
        base_size_bias=1.0, base_confidence=0.55,
        action="BUY",
    )
    assert sb_buy == pytest.approx(0.3)
    assert damp_buy["floored_size_bias_from"] == pytest.approx(0.18)
    # HOLD case: same 0.18 → NOT floored (HOLD has no directional
    # footprint; the ladder ignores its size_bias anyway).
    sb_hold, _, damp_hold = _finalise_size_and_confidence(
        final_size_bias=0.18, final_confidence=0.30,
        base_size_bias=1.0, base_confidence=0.55,
        action="HOLD",
    )
    assert sb_hold == pytest.approx(0.18)
    assert "floored_size_bias_from" not in damp_hold


def test_finalizer_floor_skips_when_already_zero(monkeypatch):
    """When the wrapper zeroed size_bias (HOLD branches do this), the
    floor must NOT lift it — that would create exposure from a HOLD,
    violating the wrapper invariant 'never create a trade from HOLD'."""
    monkeypatch.setenv("RISEDUAL_WRAPPER_MIN_SIZE_BIAS_NONZERO", "0.3")
    sb, _, _ = _finalise_size_and_confidence(
        final_size_bias=0.0, final_confidence=0.30,
        base_size_bias=1.0, base_confidence=0.55,
        action="BUY",
    )
    # 0.0 stays 0.0 — the floor's condition is `0 < x < floor`,
    # protecting against the wrapper's HOLD-zero contract.
    assert sb == pytest.approx(0.0)


def test_finalizer_clamps_out_of_range_env(monkeypatch):
    """An operator typo (e.g., strength=99) must be clamped, not
    propagated — fail-soft on a hot path."""
    monkeypatch.setenv("RISEDUAL_WRAPPER_PENALTY_STRENGTH", "99")
    sb, _, damp = _finalise_size_and_confidence(
        final_size_bias=0.18, final_confidence=0.30,
        base_size_bias=1.0, base_confidence=0.55,
        action="BUY",
    )
    # Clamped to 1.0 — pass-through.
    assert damp["penalty_strength"] == 1.0
    assert sb == pytest.approx(0.18)


def test_finalizer_handles_bad_env_gracefully(monkeypatch):
    """Non-numeric env value falls back to default — never raises."""
    monkeypatch.setenv("RISEDUAL_WRAPPER_PENALTY_STRENGTH", "not-a-number")
    sb, _, damp = _finalise_size_and_confidence(
        final_size_bias=0.18, final_confidence=0.30,
        base_size_bias=1.0, base_confidence=0.55,
        action="BUY",
    )
    assert damp["penalty_strength"] == 1.0
    assert sb == pytest.approx(0.18)


# ── end-to-end through actual wrappers ────────────────────────────


def test_alpha_wrapper_dampener_disabled_recovers_size(monkeypatch):
    """With strength=0.0 the alpha wrapper must NOT compress size_bias
    below the base intent — operator can run "wrapper-bypass" mode
    when they suspect the legacy penalties are stale."""
    monkeypatch.setenv("RISEDUAL_WRAPPER_PENALTY_STRENGTH", "0.0")
    result = apply_alpha_legacy_executor(_heavy_penalty_buy_intent())
    assert result["action"] == "BUY"
    assert result["size_bias"] == pytest.approx(1.0), (
        "strength=0 must restore the base size_bias"
    )
    # The wrapper's WARNINGS are preserved (they're diagnostic, not
    # an action) — the operator can still SEE that the wrapper would
    # have wanted to compress. They just chose not to listen.
    assert "ALPHA_WRAPPER_POSITION_STATE_UNKNOWN" in result["warnings"]


def test_alpha_wrapper_floor_protects_directional(monkeypatch):
    """With a 0.3 floor, the heavy-penalty BUY (which would normally
    crush size_bias) is clamped UP — the intent gets a minimum
    executable footprint instead of being functionally muted."""
    monkeypatch.setenv("RISEDUAL_WRAPPER_MIN_SIZE_BIAS_NONZERO", "0.3")
    result = apply_alpha_legacy_executor(_heavy_penalty_buy_intent())
    assert result["size_bias"] >= 0.3, (
        f"floor must clamp BUY size_bias UP to 0.3 (got {result['size_bias']})"
    )
    damp = result["evidence"]["legacy_wrapper"]["dampener"]
    assert damp["min_size_bias_nonzero"] == pytest.approx(0.3)


def test_alpha_wrapper_default_preserves_current_behavior(monkeypatch):
    """Critical regression check: with NO env knobs set, the wrapper
    must produce IDENTICAL output to pre-dampener behavior — the
    dampener is opt-in, never silently active."""
    monkeypatch.delenv("RISEDUAL_WRAPPER_PENALTY_STRENGTH", raising=False)
    monkeypatch.delenv("RISEDUAL_WRAPPER_MIN_SIZE_BIAS_NONZERO", raising=False)
    intent = _heavy_penalty_buy_intent()
    result = apply_alpha_legacy_executor(intent)
    # Alpha applies: unknown_position (×0.70) + weak_commitment_open_long (×0.85)
    # Starting size_bias=1.0 → 1.0 × 0.70 × 0.85 = 0.595
    assert result["size_bias"] == pytest.approx(0.595, abs=0.001), (
        "default config must preserve the pre-dampener stacked penalty"
    )


def test_chevelle_wrapper_dampener_works(monkeypatch):
    """Chevelle's wrapper has the heaviest compression (FLIP ×0.35,
    RISK_OFF_OPEN ×0.50) — the operator's primary motivation for
    the dampener. Verify strength=0.5 lifts a FLIP from ×0.35 to a
    more usable footprint."""
    monkeypatch.setenv("RISEDUAL_WRAPPER_PENALTY_STRENGTH", "0.5")
    intent = {
        "brain_id": "hellcat", "display_name": "Hellcat",
        "action": "BUY", "confidence": 0.70, "size_bias": 1.0,
        "current_side": "SHORT",
        "transition_intent": "FLIP_SHORT_TO_LONG",
        "position_evolution": None, "risk_transition": None,
        "evidence": {},
    }
    result = apply_chevelle_legacy_governor(intent)
    # FLIP penalty: 1.0 × 0.35 = 0.35; at strength=0.5 the deviation
    # (-0.65) is halved → 1.0 + (-0.325) = 0.675.
    assert result["size_bias"] == pytest.approx(0.675, abs=0.001)


def test_camaro_wrapper_hold_stays_zero_under_dampener(monkeypatch):
    """A HOLD intent through camaro must produce size_bias=0 even
    with a non-zero floor — the wrapper's HOLD-zero contract is
    sacred (the floor never lifts a HOLD to a directional footprint)."""
    monkeypatch.setenv("RISEDUAL_WRAPPER_MIN_SIZE_BIAS_NONZERO", "0.3")
    intent = {
        "brain_id": "barracuda", "display_name": "Barracuda",
        "action": "HOLD", "confidence": 0.40, "size_bias": 1.0,
        "current_side": None, "transition_intent": None,
        "position_evolution": None, "risk_transition": None,
        "evidence": {"market_regime": "chop"},
    }
    result = apply_camaro_legacy_strategist(intent)
    assert result["action"] == "HOLD"
    assert result["size_bias"] == 0.0, (
        "HOLD must stay 0 — floor only applies to BUY/SELL"
    )


def test_redeye_wrapper_diagnostics_stamped(monkeypatch):
    """The dampener diagnostics must land in evidence.legacy_wrapper
    so the operator can see what the dampener did per-intent."""
    monkeypatch.setenv("RISEDUAL_WRAPPER_PENALTY_STRENGTH", "0.7")
    intent = {
        "brain_id": "gto", "display_name": "GTO",
        "action": "BUY", "confidence": 0.50, "size_bias": 1.0,
        "current_side": "UNKNOWN",
        "transition_intent": "OPEN_LONG",
        "position_evolution": None,
        "risk_transition": "RISK_OFF",  # triggers REDEYE_WRAPPER_LONG_AGAINST_RISK_OFF
        "evidence": {"buy_score": 0.5, "sell_score": 0.5,
                     "flow_imbalance": -0.30},
    }
    result = apply_redeye_legacy_adversary(intent)
    damp = result["evidence"]["legacy_wrapper"]["dampener"]
    assert damp["penalty_strength"] == pytest.approx(0.7)
    assert "pre_dampener_size_bias" in damp
    assert "pre_dampener_confidence" in damp


def test_all_four_wrappers_default_unchanged_by_dampener(monkeypatch):
    """Tripwire: under default env (no knobs set), every wrapper's
    output must match what it would have produced before the dampener
    landed. The pre-dampener_* diagnostics keys must NOT appear."""
    monkeypatch.delenv("RISEDUAL_WRAPPER_PENALTY_STRENGTH", raising=False)
    monkeypatch.delenv("RISEDUAL_WRAPPER_MIN_SIZE_BIAS_NONZERO", raising=False)
    for wrapper in (
        apply_alpha_legacy_executor,
        apply_chevelle_legacy_governor,
        apply_camaro_legacy_strategist,
        apply_redeye_legacy_adversary,
    ):
        result = wrapper(_heavy_penalty_buy_intent())
        damp = result["evidence"]["legacy_wrapper"]["dampener"]
        # Pass-through configuration → no diagnostics drift.
        assert damp == {"penalty_strength": 1.0, "min_size_bias_nonzero": 0.0}, (
            f"{wrapper.__name__} produced unexpected dampener diagnostics "
            f"under default config: {damp}"
        )
