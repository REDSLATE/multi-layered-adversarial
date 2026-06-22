"""Tests for the camaro + redeye wrappers' squeeze-aware modulation."""
from __future__ import annotations

import pytest

from shared.legacy_brain_wrappers import (
    apply_camaro_legacy_doctrine,
    apply_redeye_legacy_doctrine,
)


def _intent_with_squeeze(action: str, sq_grade: str, sq_risks=None, base_conf=0.60):
    return {
        "brain_id": "camaro",
        "display_name": "Barracuda",
        "action": action,
        "confidence": base_conf,
        "size_bias": 1.0,
        "current_side": "FLAT",
        "transition_intent": "OPEN_LONG" if action == "BUY" else "OPEN_SHORT",
        "position_evolution": "OPEN",
        "risk_transition": "NEUTRAL",
        "reasons": [],
        "warnings": [],
        "evidence": {
            "snapshot": {
                "squeeze": {
                    "grade": sq_grade,
                    "action_bias": "SQUEEZE_CANDIDATE" if sq_grade == "A" else "OK",
                    "risk_flags": sq_risks or [],
                },
            },
        },
    }


def test_camaro_squeeze_A_buy_boosts_confidence():
    base = _intent_with_squeeze("BUY", "A")
    res = apply_camaro_legacy_doctrine(base)
    assert res["confidence"] > 0.60
    assert any("SQUEEZE_A_TAPE_CONFIRMED" in r for r in res["reasons"])


def test_camaro_squeeze_F_data_error_compresses_everything():
    base = _intent_with_squeeze("BUY", "F")
    res = apply_camaro_legacy_doctrine(base)
    assert res["confidence"] < 0.60
    assert res["size_bias"] < 1.0
    assert any("SQUEEZE_DATA_ERROR_OR_STALE" in w for w in res["warnings"])


def test_camaro_already_fading_from_high_no_chase():
    base = _intent_with_squeeze("BUY", "B", sq_risks=["already_fading_from_high"])
    res = apply_camaro_legacy_doctrine(base)
    assert res["size_bias"] <= 0.6  # heavy compression
    assert any("FADING_FROM_HIGH_NO_CHASE" in w for w in res["warnings"])


def test_camaro_wide_spread_compresses_size():
    base = _intent_with_squeeze("BUY", "B", sq_risks=["wide_spread_risk"])
    res = apply_camaro_legacy_doctrine(base)
    assert res["size_bias"] < 1.0
    assert any("WIDE_SPREAD_COMPRESSED" in w for w in res["warnings"])


def test_redeye_squeeze_A_buy_compresses_crowded_long():
    base = _intent_with_squeeze("BUY", "A")
    base["brain_id"] = "redeye"
    base["display_name"] = "GTO"
    res = apply_redeye_legacy_doctrine(base)
    assert res["confidence"] < 0.60
    assert any("SQUEEZE_A_CROWDED_LONG_SUSPECT" in w for w in res["warnings"])


def test_redeye_squeeze_A_sell_gets_failed_breakout_boost():
    base = _intent_with_squeeze("SELL", "A")
    base["brain_id"] = "redeye"
    base["display_name"] = "GTO"
    res = apply_redeye_legacy_doctrine(base)
    assert res["confidence"] > 0.60
    assert any("FAILED_BREAKOUT_OPPORTUNITY" in r for r in res["reasons"])


def test_redeye_fading_from_high_supports_short():
    base = _intent_with_squeeze("SELL", "B", sq_risks=["already_fading_from_high"])
    base["brain_id"] = "redeye"
    base["display_name"] = "GTO"
    res = apply_redeye_legacy_doctrine(base)
    assert res["confidence"] > 0.60
    assert any("FADING_FROM_HIGH_SHORT_THESIS" in r for r in res["reasons"])


def test_redeye_blowoff_velocity_supports_sell():
    base = _intent_with_squeeze("SELL", "C", sq_risks=["blowoff_velocity_risk"])
    base["brain_id"] = "redeye"
    base["display_name"] = "GTO"
    res = apply_redeye_legacy_doctrine(base)
    assert any("BLOWOFF_REVERSAL_TARGET" in r for r in res["reasons"])


def test_no_squeeze_block_is_no_op():
    """When intent.evidence has no `snapshot.squeeze`, wrappers must
    behave as before — no exceptions, no spurious modulation."""
    base = {
        "brain_id": "camaro",
        "display_name": "Barracuda",
        "action": "BUY",
        "confidence": 0.55,
        "size_bias": 1.0,
        "current_side": "FLAT",
        "transition_intent": "OPEN_LONG",
        "position_evolution": "OPEN",
        "risk_transition": "NEUTRAL",
        "reasons": [],
        "warnings": [],
        "evidence": {},  # no snapshot.squeeze
    }
    res = apply_camaro_legacy_doctrine(base)
    # No squeeze-related warnings or reasons should appear
    sq_reasons = [r for r in res["reasons"] if "SQUEEZE" in r]
    sq_warns = [w for w in res["warnings"] if "SQUEEZE" in w]
    assert sq_reasons == []
    assert sq_warns == []
