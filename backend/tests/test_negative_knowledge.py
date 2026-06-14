"""NegativeKnowledge tests — Paradox v2."""
from __future__ import annotations

from brains.negative_knowledge import NegativeKnowledge


def test_empty_store_never_abstains():
    nk = NegativeKnowledge(brain_id="alpha")
    should_abstain, reason = nk.check("setup_aabbccdd", regime="trending")
    assert should_abstain is False
    assert reason is None


def test_learn_then_check_triggers_abstention():
    nk = NegativeKnowledge(brain_id="alpha")
    nk.learn_from_failure(
        setup_embedding="setup_aabbccdd_v1",
        regime="trending",
        loss_bps=-120.0,
    )
    should_abstain, reason = nk.check("setup_aabbccdd_v1", regime="trending")
    assert should_abstain is True
    assert "negative_pattern" in reason
    assert "regret=120" in reason


def test_regime_mismatch_does_not_trigger():
    """Same setup, different regime → no abstain. Regime-aware by design."""
    nk = NegativeKnowledge(brain_id="alpha")
    nk.learn_from_failure("setup_aabbccdd_v1", regime="trending", loss_bps=-200)
    should_abstain, _ = nk.check("setup_aabbccdd_v1", regime="choppy")
    assert should_abstain is False


def test_learn_reinforces_existing_pattern():
    nk = NegativeKnowledge(brain_id="alpha")
    nk.learn_from_failure("setup_xxxx", regime="r", loss_bps=-50)
    nk.learn_from_failure("setup_xxxx", regime="r", loss_bps=-80)
    patterns = nk.patterns_for_regime("r")
    assert len(patterns) == 1
    assert patterns[0].false_positive_count == 2
    assert patterns[0].regret_score == 130.0


def test_distinct_patterns_recorded_separately():
    nk = NegativeKnowledge(brain_id="alpha")
    nk.learn_from_failure("alphabetagamma", regime="r", loss_bps=-50)
    nk.learn_from_failure("zetaetatheta", regime="r", loss_bps=-50)
    assert nk.pattern_count() == 2


def test_low_similarity_does_not_trigger():
    nk = NegativeKnowledge(brain_id="alpha", similarity_threshold=0.85)
    nk.learn_from_failure("alphabetagamma", regime="r", loss_bps=-100)
    # First 4 chars match → similarity=0.5 < threshold=0.85 → no abstain.
    should_abstain, _ = nk.check("alphazetatheta", regime="r")
    assert should_abstain is False
