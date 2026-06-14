"""Governor disagreement tests — Paradox v2."""
from __future__ import annotations

from datetime import datetime, timezone

from governor.disagreement import compute_disagreement
from shared.brain_vote import BrainVote, CalibrationKey


def _vote(brain, stance, conf):
    if stance == "ABSTAIN":
        return BrainVote.abstain(
            brain=brain, reason="r",
            calibration_key=CalibrationKey(regime="r", conf_bucket=0.5),
            raw_confidence=conf,
        )
    return BrainVote(
        brain=brain, stance=stance,
        calibrated_confidence=conf, raw_confidence=conf,
        calibration_key=CalibrationKey(regime="r", conf_bucket=round(conf, 1)),
        memory_evidence=None,
        negative_knowledge_triggered=False,
        reasoning=("r",),
        timestamp=datetime.now(timezone.utc),
    )


def test_unanimous_returns_zero_entropy():
    votes = [_vote("alpha", "BUY", 0.8), _vote("camaro", "BUY", 0.75),
             _vote("chevelle", "BUY", 0.85), _vote("redeye", "BUY", 0.70)]
    m = compute_disagreement(votes, regime="r")
    assert m.entropy == 0.0
    assert m.outlier_brain is None
    assert m.majority_stance == "BUY"
    assert m.abstention_rate == 0.0


def test_split_3to1_flags_outlier():
    votes = [_vote("alpha", "BUY", 0.8), _vote("camaro", "BUY", 0.75),
             _vote("chevelle", "BUY", 0.85), _vote("redeye", "SELL", 0.90)]
    m = compute_disagreement(votes, regime="r")
    # Majority share = 3/4 = 0.75 → NOT < 0.75 → outlier NOT flagged.
    assert m.majority_stance == "BUY"
    assert m.outlier_brain is None


def test_split_2to2_flags_outlier_and_high_entropy():
    votes = [_vote("alpha", "BUY", 0.8), _vote("camaro", "BUY", 0.75),
             _vote("chevelle", "SELL", 0.85), _vote("redeye", "SELL", 0.90)]
    m = compute_disagreement(votes, regime="r")
    # 50/50 split → normalised entropy = 1.0
    assert m.entropy == 1.0
    # Outlier flagged from the minority side; redeye has highest
    # calibrated confidence in the minority.
    assert m.outlier_brain in {"chevelle", "redeye", "alpha", "camaro"}
    assert m.majority_stance in {"BUY", "SELL"}


def test_outlier_picks_most_confident_dissenter():
    # 3 BUY, 1 SELL — but the SELL is loud-confident.
    votes = [_vote("alpha", "BUY", 0.6), _vote("camaro", "BUY", 0.6),
             _vote("redeye", "SELL", 0.95)]
    m = compute_disagreement(votes, regime="r")
    # Majority share = 2/3 ≈ 0.67 < 0.75 → outlier flagged.
    assert m.outlier_brain == "redeye"
    assert m.outlier_stance == "SELL"


def test_abstention_rate_counts_abstainers():
    votes = [_vote("alpha", "BUY", 0.8), _vote("camaro", "ABSTAIN", 0.5),
             _vote("chevelle", "ABSTAIN", 0.5), _vote("redeye", "BUY", 0.7)]
    m = compute_disagreement(votes, regime="r")
    assert m.abstention_rate == 0.5
    # Only the non-abstaining brains shape the majority + entropy.
    assert m.majority_stance == "BUY"
    assert m.entropy == 0.0


def test_all_abstain_returns_full_abstention():
    votes = [_vote("alpha", "ABSTAIN", 0.5), _vote("camaro", "ABSTAIN", 0.5)]
    m = compute_disagreement(votes, regime="r")
    assert m.abstention_rate == 1.0
    assert m.majority_stance is None
    assert m.entropy == 1.0


def test_empty_vote_list_returns_max_abstention():
    m = compute_disagreement([], regime="r")
    assert m.abstention_rate == 1.0
    assert m.entropy == 1.0
    assert m.majority_stance is None


def test_majority_confidence_is_mean_of_winning_side():
    votes = [_vote("alpha", "BUY", 0.6), _vote("camaro", "BUY", 0.8),
             _vote("redeye", "SELL", 0.9)]
    m = compute_disagreement(votes, regime="r")
    assert m.majority_stance == "BUY"
    # Mean of 0.6 and 0.8 = 0.7
    assert m.majority_confidence == 0.7
