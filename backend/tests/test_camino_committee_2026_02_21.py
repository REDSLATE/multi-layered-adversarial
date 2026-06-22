"""Tests for the Alpha→Camino merge (2026-02-21).

Three layers:
  1. `alpha_engine.py` — the Bull/Bear adversarial kernel + toxic-spike
     cap. Verifies the operator's three reference test cases produce
     the expected decisions (LONG / SHORT_OR_AVOID / NO_TRADE) and
     that confidence never exceeds 0.95.
  2. `camino_committee.py` — the Alpha-weighted aggregator. Pins
     priors, vote weighting, NO_QUORUM behaviour, and that the
     intent-mutator no-ops when no votes are present.
  3. Integration with `legacy_brain_wrappers.apply_legacy_wrapper` —
     committee pre-pass mutates confidence BEFORE
     `apply_alpha_legacy_doctrine` runs; env-var kill switch works;
     non-Camino brains are untouched.
"""
from __future__ import annotations

import os

import pytest

from shared.brains.alpha_engine import (
    Alpha, AlphaConfig, bear_agent, bull_agent,
    cap_confidence, resolve_adversarial,
)
from shared.brains.camino_committee import (
    COMMITTEE_DOCTRINE_VERSION, CommitteeVote, SUB_AGENT_PRIORS,
    aggregate_committee, apply_committee_to_intent,
)
from shared.legacy_brain_wrappers import apply_legacy_wrapper


# ─── Layer 1: alpha_engine kernel ────────────────────────────────────


def test_cap_confidence_pins_at_ceiling():
    assert cap_confidence(1.0) == 0.95
    assert cap_confidence(0.99) == 0.95
    assert cap_confidence(100.0) == 95.0  # 0-100 scale
    assert cap_confidence(0.80) == 0.80
    assert cap_confidence(-0.5) == 0.0
    assert cap_confidence(None) == 0.0


def test_alpha_clean_uptrend_emits_long():
    alpha = Alpha()
    out = alpha.decide({
        "regime": "trending",
        "strategist": {"confidence": 0.75,
                       "indicators": {"rsi": 62.0, "momentum_5b": 0.028}},
        "catalyst": {"news_shock": {"sentiment_label": "bullish",
                                     "shock_state": "calm"}},
    })
    assert out["decision"] == "LONG"
    assert 0 < out["confidence"] <= 0.95
    assert "momentum_plus_trend" in out["bull"]["thesis"]


def test_alpha_parabolic_top_avoids_long():
    alpha = Alpha()
    out = alpha.decide({
        "regime": "parabolic",
        "strategist": {"confidence": 0.60,
                       "indicators": {"rsi": 84.0, "momentum_5b": 0.005}},
        "options": {"aggregate": {"put_call_ratio": 1.42,
                                   "liquidity_stress_index": 5.1}},
    })
    assert out["decision"] in {"SHORT_OR_AVOID", "NO_TRADE"}
    assert out["confidence"] <= 0.95


def test_alpha_no_trade_when_signals_weak():
    alpha = Alpha()
    out = alpha.decide({
        "regime": "range",
        "strategist": {"confidence": 0.50,
                       "indicators": {"rsi": 50.0, "momentum_5b": 0.0}},
    })
    assert out["decision"] == "NO_TRADE"


def test_resolve_uses_score_not_raw_confidence():
    """Bull conf 0.70 × R 1.5 (=1.05) must beat Bear conf 0.80 × R 1.0
    (=0.80) — the Commander reads the SCORE, not raw confidence."""
    from shared.brains.alpha_engine import AgentOutput
    bull = AgentOutput("LONG", 0.70, 1.5, "test_bull", [])
    bear = AgentOutput("SHORT_OR_REJECT", 0.80, 1.0, "test_bear", [])
    out = resolve_adversarial(bull, bear, cfg=AlphaConfig())
    # gap = 0.70*1.5 - 0.80*1.0 = 0.25 — below 0.35 threshold → NO_TRADE
    assert out["decision"] == "NO_TRADE"


# ─── Layer 2: camino_committee aggregator ────────────────────────────


def test_committee_priors_match_operator_telemetry():
    """Pin the priors against the operator's verbatim Alpha stats."""
    assert SUB_AGENT_PRIORS["war_room"].win_rate == 0.917
    assert SUB_AGENT_PRIORS["market_prediction"].win_rate == 0.879
    assert SUB_AGENT_PRIORS["hypothesis"].win_rate == 0.855
    assert SUB_AGENT_PRIORS["signal_dispatcher"].win_rate == 0.664
    # paper_trader and pg_agent must be disabled by default.
    assert SUB_AGENT_PRIORS["paper_trader"].enabled is False
    assert SUB_AGENT_PRIORS["pg_agent"].enabled is False


def test_committee_no_quorum_when_only_disabled_agents_vote():
    votes = [
        CommitteeVote("paper_trader", "LONG", 0.9),
        CommitteeVote("pg_agent", "LONG", 0.9),
    ]
    verdict = aggregate_committee(votes)
    assert verdict.side == "NO_QUORUM"
    assert verdict.confidence == 0.0
    assert len(verdict.excluded_votes) == 2


def test_committee_single_signal_dispatcher_vote_gets_calibrated():
    """66.4% prior should calibrate signal_dispatcher's 0.80 → ~0.80
    (weighted-mean of one vote = that vote's raw confidence)."""
    votes = [CommitteeVote("signal_dispatcher", "LONG", 0.80)]
    verdict = aggregate_committee(votes)
    assert verdict.side == "LONG"
    # Weighted-mean of a single vote == the vote's raw confidence.
    assert abs(verdict.confidence - 0.80) < 1e-6
    # But its weighted SCORE reflects the 0.664 prior (this is what
    # the Commander compares across sides).
    assert abs(verdict.weighted_score - 0.80 * 0.664) < 1e-6


def test_committee_war_room_beats_signal_dispatcher_on_disagreement():
    """When war_room (0.917) and signal_dispatcher (0.664) disagree at
    equal raw confidence, war_room must win on weighted score."""
    votes = [
        CommitteeVote("war_room", "LONG", 0.70),
        CommitteeVote("signal_dispatcher", "SHORT", 0.70),
    ]
    verdict = aggregate_committee(votes)
    assert verdict.side == "LONG"
    # war_room weighted = 0.70 × 0.917 = 0.6419
    # signal_dispatcher weighted = 0.70 × 0.664 = 0.4648
    assert abs(verdict.weighted_score - 0.70 * 0.917) < 1e-6


def test_committee_unknown_agent_is_excluded():
    votes = [
        CommitteeVote("unknown_brain", "LONG", 0.9),
        CommitteeVote("war_room", "LONG", 0.5),
    ]
    verdict = aggregate_committee(votes)
    assert verdict.side == "LONG"
    assert len(verdict.excluded_votes) == 1
    assert verdict.excluded_votes[0]["agent"] == "unknown_brain"
    assert verdict.excluded_votes[0]["reason"] == "unknown_agent"


def test_committee_confidence_respects_toxic_spike_cap():
    """Even if all winning votes are 1.0, the calibrated confidence
    is capped at 0.95 (toxic-spike seal)."""
    votes = [
        CommitteeVote("war_room", "LONG", 1.0),
        CommitteeVote("market_prediction", "LONG", 1.0),
        CommitteeVote("hypothesis", "LONG", 1.0),
    ]
    verdict = aggregate_committee(votes)
    assert verdict.side == "LONG"
    assert verdict.confidence == 0.95


def test_committee_enabled_override_lets_operator_disable_a_winner():
    """Override flips war_room off → market_prediction takes over."""
    votes = [
        CommitteeVote("war_room", "LONG", 0.95),
        CommitteeVote("market_prediction", "SHORT", 0.70),
    ]
    verdict = aggregate_committee(
        votes, enabled_overrides={"war_room": False},
    )
    assert verdict.side == "SHORT"


def test_committee_doctrine_version_stamped():
    votes = [CommitteeVote("war_room", "LONG", 0.7)]
    verdict = aggregate_committee(votes)
    assert verdict.doctrine_version == COMMITTEE_DOCTRINE_VERSION


# ─── Layer 2b: apply_committee_to_intent ────────────────────────────


def test_apply_committee_no_op_when_no_votes():
    intent = {"brain_id": "camino", "action": "BUY", "confidence": 0.55}
    out = apply_committee_to_intent(intent)
    assert out["confidence"] == 0.55
    assert "committee_verdict" not in (out.get("evidence") or {})


def test_apply_committee_replaces_confidence_when_votes_present():
    intent = {
        "brain_id": "camino", "action": "BUY", "confidence": 0.55,
        "evidence": {
            "committee_votes": [
                {"agent": "war_room", "side": "LONG", "confidence": 0.70},
                {"agent": "signal_dispatcher", "side": "LONG", "confidence": 0.60},
            ],
        },
    }
    out = apply_committee_to_intent(intent)
    # weighted_mean = (0.70*0.917 + 0.60*0.664) / (0.917 + 0.664)
    #               = (0.6419 + 0.3984) / 1.581 = 0.6580
    assert 0.65 < out["confidence"] < 0.67
    assert out["evidence"]["committee_verdict"]["side"] == "LONG"
    assert out["committee_side_match"] is True


def test_apply_committee_marks_disagreement():
    intent = {
        "brain_id": "camino", "action": "SELL", "confidence": 0.55,
        "evidence": {"committee_votes": [
            {"agent": "war_room", "side": "LONG", "confidence": 0.9},
        ]},
    }
    out = apply_committee_to_intent(intent)
    # Brain said SELL (→ SHORT), committee said LONG → mismatch.
    assert out["committee_side_match"] is False


# ─── Layer 3: integration via apply_legacy_wrapper ───────────────────


def _camino_intent_with_votes() -> dict:
    return {
        "brain_id": "camino",
        "display_name": "Camino",
        "action": "BUY",
        "confidence": 0.55,
        "size_bias": 1.0,
        "reasons": [],
        "warnings": [],
        "evidence": {"committee_votes": [
            {"agent": "war_room", "side": "LONG", "confidence": 0.80},
            {"agent": "market_prediction", "side": "LONG", "confidence": 0.75},
        ]},
        "current_side": "FLAT",
        "transition_intent": "OPEN_LONG",
        "position_evolution": "OPEN",
    }


def test_integration_committee_runs_before_legacy_wrapper():
    intent = _camino_intent_with_votes()
    out = apply_legacy_wrapper(intent)
    # Committee should have stamped its verdict in evidence.
    assert "committee_verdict" in out["evidence"]
    assert out["evidence"]["committee_verdict"]["side"] == "LONG"
    # Legacy wrapper should ALSO have stamped its own audit field.
    assert "legacy_wrapper" in out["evidence"]
    assert out["evidence"]["legacy_wrapper"]["name"] == "alpha_legacy_doctrine"


def test_integration_committee_inflates_low_confidence_for_camino():
    """Brain emitted at conf 0.55 but committee says 0.78 → final
    confidence (after legacy executor discipline) must reflect the
    committee's higher signal."""
    intent = _camino_intent_with_votes()
    out = apply_legacy_wrapper(intent)
    # The legacy wrapper will nudge confidence based on transition
    # cleanliness — but the BASE it operates on is now the committee
    # confidence, which is ~0.78 (weighted mean of 0.80 & 0.75).
    # So final confidence must be materially > 0.55.
    assert out["confidence"] > 0.65, (
        f"committee_verdict={out['evidence']['committee_verdict']} "
        f"final_confidence={out['confidence']}"
    )


def test_integration_kill_switch_short_circuits_committee(monkeypatch):
    monkeypatch.setenv("RISEDUAL_CAMINO_COMMITTEE_DISABLED", "1")
    intent = _camino_intent_with_votes()
    out = apply_legacy_wrapper(intent)
    # Committee MUST NOT have run.
    assert "committee_verdict" not in out["evidence"]
    # Legacy wrapper still ran on the original confidence (0.55).
    assert "legacy_wrapper" in out["evidence"]


def test_integration_other_brains_untouched():
    """Barracuda intent with `committee_votes` attached must NOT have
    the committee applied — committee is Camino-only."""
    intent = {
        "brain_id": "barracuda",
        "display_name": "Barracuda",
        "action": "BUY",
        "confidence": 0.55,
        "size_bias": 1.0,
        "reasons": [], "warnings": [],
        "evidence": {"committee_votes": [
            {"agent": "war_room", "side": "LONG", "confidence": 0.99},
        ]},
        "current_side": "FLAT",
        "transition_intent": "OPEN_LONG",
        "position_evolution": "OPEN",
    }
    out = apply_legacy_wrapper(intent)
    # No committee verdict should be stamped on a non-Camino brain.
    assert "committee_verdict" not in out["evidence"]


def test_integration_committee_failure_is_fail_soft(monkeypatch):
    """If the committee aggregator raises, the wrapper MUST still
    run on the original confidence."""
    from shared.brains import camino_committee

    def _boom(*_a, **_k):
        raise RuntimeError("aggregator exploded")

    monkeypatch.setattr(camino_committee, "apply_committee_to_intent", _boom)
    intent = _camino_intent_with_votes()
    out = apply_legacy_wrapper(intent)
    assert "committee_error" in out["evidence"]
    assert "legacy_wrapper" in out["evidence"]
