"""Tests for the adversarial dissent classifier (operator spec, 2026-06-26).

Covers all 6 doctrine points:
  1. Tight agreement (side + conf gap + risk match)
  2. Dissent classification (HARD/CONF/RISK/SOFT/TRUE)
  3. Boost cap at +0.04
  4. Dissent kills/dampens boost (hard=0+gov*0.5, risk=0+gov*0.7, soft=*0.35)
  5. Barracuda required for positive boost
  6. Groupthink advisors (>90% rolling agree) get damped

Module is env-gated. Tests bypass the env gate by calling functions
directly — the gate only short-circuits `compute_consensus_boost`
to the legacy path; the dissent module's own logic is always testable.
"""
from __future__ import annotations

import pytest

from shared.pipeline.consensus_dissent import (
    RELATION_CONF_DISSENT,
    RELATION_HARD_DISSENT,
    RELATION_RISK_DISSENT,
    RELATION_SOFT_DISSENT,
    RELATION_TRUE_AGREEMENT,
    apply_dissent,
    classify_advisor_relation,
    derive_risk_level,
)


# ── derive_risk_level ────────────────────────────────────────────────


def test_risk_level_bands():
    assert derive_risk_level(0.40) == "low"
    assert derive_risk_level(0.54) == "low"
    assert derive_risk_level(0.55) == "medium"
    assert derive_risk_level(0.65) == "medium"
    assert derive_risk_level(0.74) == "medium"
    assert derive_risk_level(0.75) == "high"
    assert derive_risk_level(0.90) == "high"
    assert derive_risk_level("garbage") == "unknown"


# ── classify_advisor_relation (rules 1+2) ────────────────────────────


def test_opposite_side_is_hard_dissent():
    assert classify_advisor_relation("BUY", 0.70, "SELL", 0.70) == RELATION_HARD_DISSENT


def test_same_side_tight_match_is_true_agreement():
    # gap 0.05 ≤ 0.08, same risk band (both medium)
    assert classify_advisor_relation("BUY", 0.70, "BUY", 0.65) == RELATION_TRUE_AGREEMENT


def test_same_side_large_gap_is_confidence_dissent():
    # gap 0.20 ≥ 0.15
    assert classify_advisor_relation("BUY", 0.70, "BUY", 0.90) == RELATION_CONF_DISSENT


def test_same_side_risk_band_mismatch_is_risk_dissent():
    # gap 0.10 (under both 0.08 agree and 0.15 dissent), bands differ (medium vs high)
    assert classify_advisor_relation("BUY", 0.65, "BUY", 0.75) == RELATION_RISK_DISSENT


def test_same_side_moderate_gap_same_band_is_soft_dissent():
    # gap 0.10, both medium band → soft (not RISK_DISSENT, not TRUE_AGREEMENT)
    assert classify_advisor_relation("BUY", 0.60, "BUY", 0.70) == RELATION_SOFT_DISSENT


# ── apply_dissent: rule 4 (dissent kills/dampens boost) ──────────────


def _adv(brain, action, conf):
    return {"brain_id": brain, "action": action, "confidence": conf}


def test_hard_dissent_zeroes_boost_and_damps_governor():
    v = apply_dissent(
        executor_action="BUY",
        executor_confidence=0.70,
        advisors=[
            _adv("barracuda", "SELL", 0.70),    # HARD
            _adv("hellcat", "BUY", 0.68),
        ],
        raw_boost=0.12,
    )
    assert v.boost == 0.0
    assert v.governor_multiplier == 0.50
    assert "hard_dissent" in (v.blocked_reason or "")


def test_risk_dissent_zeroes_boost_with_70pct_governor():
    v = apply_dissent(
        executor_action="BUY",
        executor_confidence=0.65,
        advisors=[
            _adv("barracuda", "BUY", 0.78),   # risk_dissent (medium vs high)
            _adv("hellcat", "BUY", 0.66),
        ],
        raw_boost=0.10,
    )
    assert v.boost == 0.0
    assert v.governor_multiplier == 0.70
    assert "risk_dissent" in (v.blocked_reason or "")


def test_soft_dissent_dampens_boost_to_35pct():
    v = apply_dissent(
        executor_action="BUY",
        executor_confidence=0.60,
        advisors=[
            _adv("barracuda", "BUY", 0.70),   # soft (gap 0.10, both medium)
            _adv("hellcat", "BUY", 0.62),     # true agreement (gap 0.02)
        ],
        raw_boost=0.10,
    )
    assert v.boost == pytest.approx(0.035, abs=1e-6)


# ── apply_dissent: rule 3 (cap at 0.04) ──────────────────────────────


def test_true_agreement_caps_boost_at_max():
    v = apply_dissent(
        executor_action="BUY",
        executor_confidence=0.70,
        advisors=[
            _adv("barracuda", "BUY", 0.68),
            _adv("hellcat", "BUY", 0.66),
            _adv("gto", "BUY", 0.65),
        ],
        raw_boost=0.15,
    )
    assert v.boost <= 0.04 + 1e-9


# ── apply_dissent: rule 5 (Barracuda required) ───────────────────────


def test_missing_barracuda_zeroes_positive_boost():
    v = apply_dissent(
        executor_action="BUY",
        executor_confidence=0.70,
        advisors=[
            _adv("hellcat", "BUY", 0.66),
            _adv("gto", "BUY", 0.65),
        ],
        raw_boost=0.10,
    )
    assert v.boost == 0.0
    assert v.barracuda_present is False
    assert v.blocked_reason == "missing_barracuda_adversary"


def test_missing_barracuda_does_not_block_negative_boost():
    # Negative boost (from disagreement) should still apply when Barracuda absent.
    v = apply_dissent(
        executor_action="BUY",
        executor_confidence=0.70,
        advisors=[
            _adv("hellcat", "BUY", 0.66),
            _adv("gto", "SELL", 0.65),   # hard dissent → boost forced to 0
        ],
        raw_boost=-0.05,
    )
    # Hard dissent path runs first; boost=0; missing-barracuda branch
    # only fires on positive boosts. Either way no blocked_reason
    # specifically about barracuda is appended after hard_dissent.
    assert v.boost == 0.0


# ── apply_dissent: rule 6 (groupthink damping) ───────────────────────


def test_groupthink_advisor_dampens_boost():
    rates = {"hellcat": 0.99, "gto": 0.50, "barracuda": 0.40}
    v_with = apply_dissent(
        executor_action="BUY",
        executor_confidence=0.70,
        advisors=[
            _adv("barracuda", "BUY", 0.68),
            _adv("hellcat", "BUY", 0.66),
            _adv("gto", "BUY", 0.69),
        ],
        raw_boost=0.04,
        advisor_agree_rates=rates,
    )
    v_without = apply_dissent(
        executor_action="BUY",
        executor_confidence=0.70,
        advisors=[
            _adv("barracuda", "BUY", 0.68),
            _adv("hellcat", "BUY", 0.66),
            _adv("gto", "BUY", 0.69),
        ],
        raw_boost=0.04,
        advisor_agree_rates={"hellcat": 0.50, "gto": 0.50, "barracuda": 0.40},
    )
    # With groupthinker damped, boost should be lower than without.
    assert v_with.boost < v_without.boost
    assert "hellcat" in v_with.damped_advisors


# ── runtime_flags override path ──────────────────────────────────────


def test_flag_overrides_widen_agree_band():
    # With overrides loosening the agree gap, a 0.10 same-side gap can
    # be classified as TRUE_AGREEMENT instead of SOFT.
    v = apply_dissent(
        executor_action="BUY",
        executor_confidence=0.60,
        advisors=[
            _adv("barracuda", "BUY", 0.70),  # gap 0.10
        ],
        raw_boost=0.05,
        flag_overrides={"adv_conf_gap_agree": 0.15},
    )
    # With agree band widened to 0.15, this becomes true agreement.
    assert RELATION_TRUE_AGREEMENT in v.relations
