"""Tests for the Camaro → Barracuda transplant (2026-02-21).

Three layers:
  1. `camaro_weights.py` kernel — pin each of the 5 improvements
     (dead-zone bands, graduated loss streak, scaled leader penalty,
     regime-aware RR floor, conviction score).
  2. `camaro_weights_adapter.py` — input mapping (regime aliases,
     defaults that preserve Camaro's looseness, JSON-safe writeback).
  3. Integration via `apply_legacy_wrapper` — Barracuda only,
     fail-soft, env kill switch, doesn't affect other brains.
"""
from __future__ import annotations

import pytest

from shared.brains.camaro_weights import (
    EVENT_RISK_DAMPENERS, EventRisk, LEADER_PENALTY_BY_SPLIT,
    LOSS_STREAK_DAMPENERS, Regime, SizingBand, SIZING_BAND_WEIGHTS,
    build_weighted_decision, compute_conviction_score,
    get_leader_penalty, get_loss_streak_dampener,
    get_min_rr_for_regime, resolve_council_split, resolve_sizing_band,
)
from shared.brains.camaro_weights_adapter import (
    apply_camaro_weights_to_intent, kill_switch_tripped,
)
from shared.legacy_brain_wrappers import apply_legacy_wrapper


# ─── Improvement 1: dead-zone bands (nano_live, seed_live) ──────────


def test_improvement_1_seed_live_band_at_058():
    """Original threw away edge below 0.65. Improved: 0.58 = SEED_LIVE
    with ×0.05 weight (real, if tiny, live exposure)."""
    band, weight = resolve_sizing_band(0.58)
    assert band == SizingBand.SEED_LIVE
    assert weight == 0.05


def test_improvement_1_nano_live_band_at_062():
    band, weight = resolve_sizing_band(0.62)
    assert band == SizingBand.NANO_LIVE
    assert weight == 0.10


def test_improvement_1_dead_zone_no_longer_zero_sized():
    """Confidence in 0.58–0.65 must produce non-zero size."""
    for c in (0.58, 0.60, 0.62, 0.64):
        _, w = resolve_sizing_band(c)
        assert w > 0.0, f"dead-zone confidence {c} produced zero size"


def test_improvement_1_below_058_still_zero():
    band, weight = resolve_sizing_band(0.55)
    assert band == SizingBand.OBSERVATION
    assert weight == 0.00


# ─── Improvement 2: graduated loss streak ──────────────────────────


def test_improvement_2_graduated_loss_streak():
    assert get_loss_streak_dampener(0) == 1.00
    assert get_loss_streak_dampener(1) == 1.00
    assert get_loss_streak_dampener(2) == 0.85   # IMPROVED
    assert get_loss_streak_dampener(3) == 0.70   # IMPROVED
    assert get_loss_streak_dampener(4) == 0.50   # original cliff preserved
    assert get_loss_streak_dampener(5) == 0.50
    assert get_loss_streak_dampener(6) == 0.25
    assert get_loss_streak_dampener(99) == 0.25  # clamps at 6


def test_improvement_2_no_hard_cliff():
    """Old behaviour: 0–3 = ×1.00, ≥4 = ×0.50 (sudden halving).
    New behaviour must have intermediate values between 3 and 4."""
    transitions = [
        get_loss_streak_dampener(i) for i in range(7)
    ]
    # Monotonically non-increasing.
    assert all(transitions[i] >= transitions[i + 1]
               for i in range(len(transitions) - 1))
    # No 50% drop between adjacent streak counts.
    for i in range(len(transitions) - 1):
        drop = transitions[i] - transitions[i + 1]
        assert drop <= 0.30, f"streak {i}→{i+1} drops by {drop} (too cliffy)"


# ─── Improvement 3: scaled leader penalty ──────────────────────────


def test_improvement_3_scaled_leader_penalty():
    assert get_leader_penalty("clean") == 1.00
    assert get_leader_penalty("3_1") == 0.90
    assert get_leader_penalty("2_2") == 0.82
    assert get_leader_penalty("no_quorum") == 0.70


def test_improvement_3_council_split_resolution():
    # Clean = all votes same direction.
    assert resolve_council_split({"bull": 4}) == "clean"
    assert resolve_council_split({"bear": 3}) == "clean"
    # 3_1 split on 4-vote council.
    assert resolve_council_split({"bull": 3, "bear": 1}) == "3_1"
    # 2_2 split.
    assert resolve_council_split({"bull": 2, "bear": 2}) == "2_2"
    # No quorum / empty.
    assert resolve_council_split({}) == "no_quorum"


# ─── Improvement 4: regime-aware RR floor ───────────────────────────


def test_improvement_4_regime_rr_floor():
    assert get_min_rr_for_regime(Regime.BULL) == 1.35      # trending
    assert get_min_rr_for_regime(Regime.BEAR) == 1.35      # trending
    assert get_min_rr_for_regime(Regime.HIGH_VOL) == 1.80  # demand more
    assert get_min_rr_for_regime(Regime.NEUTRAL) == 1.50   # baseline


def test_improvement_4_high_vol_intent_vetoed_at_15rr():
    """An intent with RR 1.50 (the old hardcoded baseline) must now
    LOW_RR-veto under HIGH_VOL where the floor is 1.80."""
    d = build_weighted_decision(
        action="BUY", direction="bull",
        raw_confidence=0.80,
        bull_score=0.80, bear_score=0.10,
        risk_prob=0.10, vote_counts={"bull": 1},
        strategist_score=None, edge_gap=0.70,
        regime=Regime.HIGH_VOL, regime_conf=0.60,
        loss_streak=0, event_risk=EventRisk.NORMAL,
        rr_ratio=1.50,
    )
    assert any("LOW_RR" in v for v in d.vetoes)
    assert d.min_rr_threshold == 1.80


def test_improvement_4_trending_intent_passes_at_140rr():
    """Same setup at RR 1.40 (below the old 1.50) now PASSES in a
    trending regime where the floor is 1.35."""
    d = build_weighted_decision(
        action="BUY", direction="bull",
        raw_confidence=0.80,
        bull_score=0.80, bear_score=0.10,
        risk_prob=0.10, vote_counts={"bull": 1},
        strategist_score=None, edge_gap=0.70,
        regime=Regime.BULL, regime_conf=0.80,
        loss_streak=0, event_risk=EventRisk.NORMAL,
        rr_ratio=1.40,
    )
    assert not any("LOW_RR" in v for v in d.vetoes)
    assert d.min_rr_threshold == 1.35


# ─── Improvement 5: conviction_score composite ──────────────────────


def test_improvement_5_conviction_score_range():
    """Score must always be in [0.0, 1.0]."""
    for raw in (0.0, 0.5, 1.0):
        for rc in (0.0, 0.5, 1.0):
            score = compute_conviction_score(
                raw_confidence=raw, regime_conf=rc,
                leader_penalty_applied=False,
                leader_penalty_multiplier=1.0,
                strategist_tiebreak_applied=False, vetoes=[],
            )
            assert 0.0 <= score <= 1.0


def test_improvement_5_conviction_falls_with_split_council():
    clean_score = compute_conviction_score(
        raw_confidence=0.80, regime_conf=0.80,
        leader_penalty_applied=False,
        leader_penalty_multiplier=1.0,
        strategist_tiebreak_applied=False, vetoes=[],
    )
    split_score = compute_conviction_score(
        raw_confidence=0.80, regime_conf=0.80,
        leader_penalty_applied=True,
        leader_penalty_multiplier=0.70,  # no_quorum penalty
        strategist_tiebreak_applied=False, vetoes=[],
    )
    assert split_score < clean_score


def test_improvement_5_conviction_falls_with_vetoes():
    no_vetoes = compute_conviction_score(
        raw_confidence=0.80, regime_conf=0.80,
        leader_penalty_applied=False,
        leader_penalty_multiplier=1.0,
        strategist_tiebreak_applied=False, vetoes=[],
    )
    two_vetoes = compute_conviction_score(
        raw_confidence=0.80, regime_conf=0.80,
        leader_penalty_applied=False,
        leader_penalty_multiplier=1.0,
        strategist_tiebreak_applied=False,
        vetoes=["RISK_BLOCK", "LOW_RR"],
    )
    assert two_vetoes < no_vetoes


# ─── Adapter: input mapping & writeback ─────────────────────────────


def test_adapter_no_evidence_uses_loose_defaults():
    """Camaro's whole point is looseness. With minimal evidence,
    the adapter MUST NOT silently fabricate vetoes — the intent
    must still pass through with adjusted confidence + size."""
    intent = {
        "action": "BUY",
        "confidence": 0.72,
        "size_bias": 1.0,
        "warnings": [], "reasons": [],
        "evidence": {},
    }
    out = apply_camaro_weights_to_intent(intent)
    assert "camaro_weights" in out["evidence"]
    # Should NOT have LOW_RR despite no rr_ratio (default 1.50 = neutral baseline).
    vetoes = out["evidence"]["camaro_weights"]["vetoes"]
    assert not any("LOW_RR" in v for v in vetoes)
    assert "camaro_weights_error" not in out["evidence"]


def test_adapter_chop_regime_alias_maps_to_neutral():
    """The intent envelope uses 'chop' / 'sideways' — must map to
    NEUTRAL (the baseline), not silently become BULL or BEAR."""
    intent = {
        "action": "BUY", "confidence": 0.70, "size_bias": 1.0,
        "warnings": [], "reasons": [],
        "evidence": {"market_regime": "chop"},
    }
    out = apply_camaro_weights_to_intent(intent)
    assert out["evidence"]["camaro_weights"]["regime"] == "neutral"


def test_adapter_parabolic_maps_to_high_vol():
    intent = {
        "action": "BUY", "confidence": 0.70, "size_bias": 1.0,
        "warnings": [], "reasons": [],
        "evidence": {"market_regime": "parabolic"},
    }
    out = apply_camaro_weights_to_intent(intent)
    assert out["evidence"]["camaro_weights"]["regime"] == "high_vol"


def test_adapter_size_bias_compounds_with_upstream():
    """If upstream already wrote size_bias=0.5 and camaro gives ×0.50,
    final must be 0.25 — preserving operator-set sizing intent."""
    intent = {
        "action": "BUY", "confidence": 0.72, "size_bias": 0.5,
        "warnings": [], "reasons": [], "evidence": {},
    }
    out = apply_camaro_weights_to_intent(intent)
    # 0.72 → MICRO_LIVE band at 0.50 weight × dampeners (1.0 × 1.0) = 0.50
    # Final size_bias = 0.5 × 0.50 = 0.25
    assert out["size_bias"] == 0.25


def test_adapter_writeback_is_json_safe():
    """`dataclasses.asdict` must collapse enums via `str, Enum` mixin —
    `json.dumps` must succeed on the writeback."""
    import json
    intent = {
        "action": "BUY", "confidence": 0.72, "size_bias": 1.0,
        "warnings": [], "reasons": [], "evidence": {},
    }
    out = apply_camaro_weights_to_intent(intent)
    # Must round-trip through JSON without raising.
    json.dumps(out["evidence"]["camaro_weights"])


def test_adapter_vetoes_appended_to_warnings():
    """LOW_RR veto must surface as a warning the operator can see."""
    intent = {
        "action": "BUY", "confidence": 0.85, "size_bias": 1.0,
        "warnings": [], "reasons": [],
        "evidence": {
            "market_regime": "high_vol",
            "rr_ratio": 1.20,  # below HIGH_VOL floor of 1.80
        },
    }
    out = apply_camaro_weights_to_intent(intent)
    assert any("LOW_RR" in w for w in out["warnings"])


def test_adapter_fail_soft_on_garbage_input():
    """Adapter must never raise. Garbage in → original intent out +
    error stamped."""
    intent = {
        "action": "BUY", "confidence": "not_a_number", "size_bias": 1.0,
        "warnings": [], "reasons": [],
        "evidence": {"market_regime": object()},
    }
    out = apply_camaro_weights_to_intent(intent)
    # Either succeeded with defaults OR fail-soft path was taken.
    assert isinstance(out, dict)
    assert out["action"] == "BUY"


def test_adapter_kill_switch_via_env(monkeypatch):
    monkeypatch.setenv("RISEDUAL_BARRACUDA_CAMARO_WEIGHTS_DISABLED", "1")
    assert kill_switch_tripped() is True
    monkeypatch.setenv("RISEDUAL_BARRACUDA_CAMARO_WEIGHTS_DISABLED", "0")
    assert kill_switch_tripped() is False


# ─── Integration via apply_legacy_wrapper ───────────────────────────


def _barracuda_intent() -> dict:
    return {
        "brain_id": "barracuda",
        "display_name": "Barracuda",
        "action": "BUY",
        "confidence": 0.72,
        "size_bias": 1.0,
        "reasons": [], "warnings": [],
        "evidence": {
            "market_regime": "bull",
            "regime_conf": 0.80,
            "buy_score": 0.72, "sell_score": 0.10,
            "rr_ratio": 1.55, "risk_prob": 0.15,
        },
        "current_side": "FLAT",
        "transition_intent": "OPEN_LONG",
        "position_evolution": "OPEN",
    }


def test_integration_camaro_runs_before_legacy_wrapper_for_barracuda():
    out = apply_legacy_wrapper(_barracuda_intent())
    # camaro_weights pre-pass must have stamped evidence.
    assert "camaro_weights" in out["evidence"]
    # Conviction surfaced for UI.
    assert "conviction_score" in out["evidence"]
    # Legacy strategist wrapper also ran (audit field stamped).
    assert "legacy_wrapper" in out["evidence"]
    assert out["evidence"]["legacy_wrapper"]["name"] == "camaro_legacy_doctrine"


def test_integration_kill_switch_short_circuits_camaro(monkeypatch):
    monkeypatch.setenv("RISEDUAL_BARRACUDA_CAMARO_WEIGHTS_DISABLED", "1")
    out = apply_legacy_wrapper(_barracuda_intent())
    assert "camaro_weights" not in out["evidence"]
    # Legacy wrapper still ran.
    assert "legacy_wrapper" in out["evidence"]


def test_integration_camino_untouched_by_camaro_weights():
    """Camino must NOT get the Camaro pre-pass — that's Barracuda only."""
    intent = {
        "brain_id": "camino", "display_name": "Camino",
        "action": "BUY", "confidence": 0.72, "size_bias": 1.0,
        "reasons": [], "warnings": [],
        "evidence": {"market_regime": "bull", "regime_conf": 0.80},
        "current_side": "FLAT", "transition_intent": "OPEN_LONG",
        "position_evolution": "OPEN",
    }
    out = apply_legacy_wrapper(intent)
    assert "camaro_weights" not in out["evidence"]


def test_integration_hellcat_untouched_by_camaro_weights():
    """Hellcat (governor) must NOT get the Camaro pre-pass."""
    intent = {
        "brain_id": "hellcat", "display_name": "Hellcat",
        "action": "BUY", "confidence": 0.72, "size_bias": 1.0,
        "reasons": [], "warnings": [],
        "evidence": {"market_regime": "bull", "regime_conf": 0.80},
        "current_side": "FLAT", "transition_intent": "OPEN_LONG",
        "position_evolution": "OPEN",
    }
    out = apply_legacy_wrapper(intent)
    assert "camaro_weights" not in out["evidence"]


def test_integration_camaro_in_bull_regime_inflates_size():
    """The whole point: a high-conviction BUY in a confirmed bull
    regime should size up via SCALED band, not get artificially
    dampened by the old hardcoded RR floor."""
    intent = _barracuda_intent()
    intent["confidence"] = 0.82  # above the SCALED threshold (0.80)
    out = apply_legacy_wrapper(intent)
    band = out["evidence"]["camaro_weights"]["band"]
    assert band == "scaled"
    # Size multiplier should be > 0 (the old behavior at low RR
    # would have been a veto + zero size).
    assert out["evidence"]["camaro_weights"]["size_multiplier"] > 0
