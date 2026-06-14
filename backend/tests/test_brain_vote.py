"""BrainVote contract tests — Paradox v2.

Pins the immutable-record invariants the rest of the system depends on:
  * confidence bounds [0, 1]
  * symmetric calibration delta ≤ 0.30 (NOT a one-sided cap)
  * ABSTAIN requires negative_knowledge_triggered=True and conf=0.0
  * reasoning is a non-empty frozen tuple
  * the .abstain() factory enforces all of the above
"""
from __future__ import annotations

from datetime import datetime, timezone
import pytest

from shared.brain_vote import (
    BrainVote, CalibrationKey, MarketMemoryResult, CALIBRATION_DELTA_MAX,
)


def _key():
    return CalibrationKey(regime="choppy", conf_bucket=0.8)


# ─── confidence bounds ────────────────────────────────────────────────


def test_calibrated_confidence_must_be_in_range():
    with pytest.raises(ValueError, match="calibrated_confidence"):
        BrainVote(
            brain="alpha", stance="BUY",
            calibrated_confidence=1.5, raw_confidence=0.8,
            calibration_key=_key(), memory_evidence=None,
            negative_knowledge_triggered=False,
            reasoning=("x",), timestamp=datetime.now(timezone.utc),
        )


def test_raw_confidence_must_be_in_range():
    with pytest.raises(ValueError, match="raw_confidence"):
        BrainVote(
            brain="alpha", stance="BUY",
            calibrated_confidence=0.5, raw_confidence=-0.1,
            calibration_key=_key(), memory_evidence=None,
            negative_knowledge_triggered=False,
            reasoning=("x",), timestamp=datetime.now(timezone.utc),
        )


# ─── symmetric calibration delta (the operator-mandated correction) ──


def test_calibration_can_shrink_confidence():
    # Classic shrinkage: raw 0.9 → calibrated 0.65 (delta 0.25 ≤ 0.30)
    v = BrainVote(
        brain="alpha", stance="BUY",
        calibrated_confidence=0.65, raw_confidence=0.9,
        calibration_key=_key(), memory_evidence=None,
        negative_knowledge_triggered=False,
        reasoning=("shrunk to historical wr",),
        timestamp=datetime.now(timezone.utc),
    )
    assert v.calibrated_confidence == 0.65


def test_calibration_can_legitimately_inflate_confidence():
    """The earlier one-sided cap blocked this case (correctly rejected).
    A brain that is historically UNDERconfident in a regime should be
    allowed to calibrate UP (raw 0.62 → calibrated 0.68 if observed
    win rate is 0.74)."""
    v = BrainVote(
        brain="redeye", stance="BUY",
        calibrated_confidence=0.68, raw_confidence=0.62,
        calibration_key=CalibrationKey(regime="trending", conf_bucket=0.6),
        memory_evidence=None,
        negative_knowledge_triggered=False,
        reasoning=("underconfident in trending regime",),
        timestamp=datetime.now(timezone.utc),
    )
    assert v.calibrated_confidence > v.raw_confidence


def test_calibration_delta_above_threshold_is_rejected_downward():
    with pytest.raises(ValueError, match="calibration delta too large"):
        BrainVote(
            brain="alpha", stance="BUY",
            calibrated_confidence=0.10, raw_confidence=0.90,
            calibration_key=_key(), memory_evidence=None,
            negative_knowledge_triggered=False,
            reasoning=("over-shrunk",),
            timestamp=datetime.now(timezone.utc),
        )


def test_calibration_delta_above_threshold_is_rejected_upward():
    with pytest.raises(ValueError, match="calibration delta too large"):
        BrainVote(
            brain="alpha", stance="BUY",
            calibrated_confidence=0.95, raw_confidence=0.10,
            calibration_key=_key(), memory_evidence=None,
            negative_knowledge_triggered=False,
            reasoning=("gaming attempt",),
            timestamp=datetime.now(timezone.utc),
        )


def test_calibration_delta_constant_is_locked():
    assert CALIBRATION_DELTA_MAX == 0.30


# ─── abstain contract ─────────────────────────────────────────────────


def test_brain_vote_abstain_contract():
    """The operator-pinned canonical abstain test."""
    vote = BrainVote.abstain(
        brain="GTO",
        reason="choppy_regime_no_breakout",
        calibration_key=CalibrationKey(regime="choppy", conf_bucket=0.8),
        raw_confidence=0.8,
    )
    assert vote.stance == "ABSTAIN"
    assert vote.negative_knowledge_triggered is True
    assert vote.calibrated_confidence == 0.0


def test_abstain_without_negative_knowledge_is_rejected():
    with pytest.raises(ValueError, match="ABSTAIN requires"):
        BrainVote(
            brain="GTO", stance="ABSTAIN",
            calibrated_confidence=0.0, raw_confidence=0.8,
            calibration_key=_key(), memory_evidence=None,
            negative_knowledge_triggered=False,  # bug — must be True
            reasoning=("wrong",), timestamp=datetime.now(timezone.utc),
        )


def test_abstain_with_nonzero_confidence_is_rejected():
    with pytest.raises(ValueError, match="calibrated_confidence=0.0"):
        BrainVote(
            brain="GTO", stance="ABSTAIN",
            calibrated_confidence=0.3, raw_confidence=0.8,
            calibration_key=_key(), memory_evidence=None,
            negative_knowledge_triggered=True,
            reasoning=("wrong",), timestamp=datetime.now(timezone.utc),
        )


def test_abstain_factory_bypasses_delta_check():
    """ABSTAIN forces calibrated=0.0; raw can be 0.8 → delta 0.8 > 0.30.
    The delta check is gated on stance != ABSTAIN, so this must succeed."""
    v = BrainVote.abstain(
        brain="GTO",
        reason="r",
        calibration_key=_key(),
        raw_confidence=0.95,
    )
    assert v.calibrated_confidence == 0.0


# ─── reasoning + immutability ─────────────────────────────────────────


def test_reasoning_must_be_tuple_not_list():
    with pytest.raises(ValueError, match="tuple"):
        BrainVote(
            brain="alpha", stance="BUY",
            calibrated_confidence=0.7, raw_confidence=0.7,
            calibration_key=_key(), memory_evidence=None,
            negative_knowledge_triggered=False,
            reasoning=["x"],  # list, must be tuple
            timestamp=datetime.now(timezone.utc),
        )


def test_reasoning_empty_is_rejected():
    with pytest.raises(ValueError, match="at least one"):
        BrainVote(
            brain="alpha", stance="BUY",
            calibrated_confidence=0.7, raw_confidence=0.7,
            calibration_key=_key(), memory_evidence=None,
            negative_knowledge_triggered=False,
            reasoning=(), timestamp=datetime.now(timezone.utc),
        )


def test_brain_vote_is_immutable():
    v = BrainVote.abstain(
        brain="GTO", reason="r",
        calibration_key=_key(), raw_confidence=0.8,
    )
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.stance = "BUY"  # type: ignore[misc]


# ─── memory evidence ──────────────────────────────────────────────────


def test_memory_evidence_can_be_attached():
    mem = MarketMemoryResult(
        similar_count=42, win_rate=0.71,
        avg_return_bps=18.0, worst_drawdown_bps=-35.0,
        failure_pattern=None,
    )
    v = BrainVote(
        brain="alpha", stance="BUY",
        calibrated_confidence=0.7, raw_confidence=0.75,
        calibration_key=_key(), memory_evidence=mem,
        negative_knowledge_triggered=False,
        reasoning=("memory says wr=0.71",),
        timestamp=datetime.now(timezone.utc),
    )
    assert v.memory_evidence.similar_count == 42
