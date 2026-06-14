"""VerifierReplay tests — Paradox v2 failure attribution."""
from __future__ import annotations

from datetime import datetime, timezone

from shared.brain_vote import BrainVote, CalibrationKey, MarketMemoryResult
from verifier.replay import FailureType, ReplayCase, VerifierReplay


def _vote(brain, stance, conf, *, memory=None):
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
        memory_evidence=memory,
        negative_knowledge_triggered=False,
        reasoning=("r",), timestamp=datetime.now(timezone.utc),
    )


def _case(votes, direction, pnl_bps):
    return ReplayCase(
        timestamp=datetime.now(timezone.utc),
        symbol="AAPL", regime="trending",
        brain_votes={v.brain: v for v in votes},
        governor_output={"size_multiplier": 1.0, "vote_required": False},
        roadguard_decision="OPEN",
        seat_action={"direction": direction, "notional_usd": 1000},
        actual_outcome={"pnl_bps": pnl_bps},
    )


def test_shallow_loss_is_acceptable():
    case = _case([_vote("alpha", "BUY", 0.8)], "BUY", pnl_bps=-30)
    r = VerifierReplay(loss_threshold_bps=-50).analyze(case)
    assert r.type == FailureType.ACCEPTABLE_LOSS


def test_no_position_is_acceptable():
    case = _case([_vote("alpha", "HOLD", 0.8)], "HOLD", pnl_bps=-200)
    r = VerifierReplay().analyze(case)
    assert r.type == FailureType.ACCEPTABLE_LOSS
    assert "No position" in r.explanation


def test_no_supporting_brain_attributes_to_governor():
    """Loss with NO brain voting in this direction → governor error."""
    votes = [_vote("alpha", "SELL", 0.7), _vote("camaro", "HOLD", 0.5)]
    case = _case(votes, "BUY", pnl_bps=-200)
    r = VerifierReplay().analyze(case)
    assert r.type == FailureType.GOVERNOR_ERROR
    assert r.responsible_brain is None


def test_most_confident_wrong_brain_takes_attribution():
    votes = [
        _vote("alpha", "BUY", 0.6),
        _vote("camaro", "BUY", 0.85),  # the loudest wrong brain
        _vote("redeye", "SELL", 0.7),
    ]
    case = _case(votes, "BUY", pnl_bps=-150)
    r = VerifierReplay().analyze(case)
    assert r.type == FailureType.BRAIN_ERROR
    assert r.responsible_brain == "camaro"
    # 0.85 > 0.7 + -150 < -100 → calibration_error
    assert r.calibration_error is True


def test_memory_error_flagged_when_optimistic_memory():
    mem = MarketMemoryResult(
        similar_count=20, win_rate=0.75,
        avg_return_bps=12.0, worst_drawdown_bps=-30.0,
        failure_pattern=None,
    )
    votes = [_vote("alpha", "BUY", 0.8, memory=mem)]
    case = _case(votes, "BUY", pnl_bps=-200)
    r = VerifierReplay().analyze(case)
    assert r.memory_error is True


def test_negative_knowledge_miss_when_peers_abstained():
    """If majority of peers abstained but this brain voted → it
    should have abstained too. Flag as negative_knowledge_miss."""
    votes = [
        _vote("alpha", "BUY", 0.75),
        _vote("camaro", "ABSTAIN", 0.5),
        _vote("chevelle", "ABSTAIN", 0.5),
        _vote("redeye", "ABSTAIN", 0.5),
    ]
    case = _case(votes, "BUY", pnl_bps=-150)
    r = VerifierReplay().analyze(case)
    assert r.responsible_brain == "alpha"
    assert r.negative_knowledge_miss is True
