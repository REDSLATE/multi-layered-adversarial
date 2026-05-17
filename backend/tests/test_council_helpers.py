"""Unit tests for the helper functions extracted from
`_evaluate_council` during the 2026-05-17 refactor.

These are pure-function tests (no DB, no async setup) that pin the
small composable pieces of the council pipeline. Cross-check against
`test_governance_verdict.py` which covers the upstream verdict matrix.
"""
from __future__ import annotations

import pytest

from shared.council import (
    COUNCIL_POLICY,
    _build_governor_gate,
    _compose_size_with_opponent,
    _governance_verdict,
    _opponent_payload,
    _opposes_direction,
    _quantum_opinions,
)


# Tripwire suite: locked council-helper behavior. See pytest.ini.
pytestmark = pytest.mark.tripwire


EQUITY = COUNCIL_POLICY["equity"]


# ─────────────────── _opposes_direction ───────────────────

@pytest.mark.parametrize("action,side,expected", [
    ("BUY",   "bearish", True),
    ("BUY",   "short",   True),
    ("BUY",   "sell",    True),
    ("BUY",   "down",    True),
    ("COVER", "bearish", True),
    ("BUY",   "bullish", False),
    ("BUY",   "long",    False),
    ("BUY",   "buy",     False),
    ("SELL",  "bullish", True),
    ("SHORT", "long",    True),
    ("SELL",  "bearish", False),
    ("HOLD",  "bearish", False),
    ("BUY",   "",        False),
])
def test_opposes_direction(action, side, expected):
    assert _opposes_direction(action, side) is expected


# ─────────────────── _opponent_payload ───────────────────

def test_opponent_payload_pulls_from_payload_dict():
    doc = {"payload": {"confidence": 0.66, "side": "BEARISH"}}
    conf, side = _opponent_payload(doc)
    assert conf == 0.66
    assert side == "bearish"


def test_opponent_payload_falls_through_to_root():
    doc = {"side": "Bullish", "confidence": 0.42}
    conf, side = _opponent_payload(doc)
    assert conf == 0.42
    assert side == "bullish"


def test_opponent_payload_handles_non_dict_payload():
    doc = {"payload": "not-a-dict", "confidence": 0.3, "side": "short"}
    conf, side = _opponent_payload(doc)
    assert conf == 0.3
    assert side == "short"


def test_opponent_payload_zero_defaults():
    conf, side = _opponent_payload({})
    assert conf == 0.0
    assert side == ""


# ─────────────────── _compose_size_with_opponent ───────────────────

def _verdict_allowed(size: float = 1.0) -> dict:
    return {
        "allowed": True, "reason": "NO_GOVERNOR_DISSENT",
        "disagreement": False, "risk_multiplier": size,
        "effective_conf": 0.7,
    }


def _verdict_blocked() -> dict:
    return {
        "allowed": False, "reason": "GOVERNOR_HARD_VETO",
        "disagreement": True, "risk_multiplier": 0.0,
        "effective_conf": 0.0,
    }


def test_compose_size_passes_through_when_opponent_silent():
    verdict = _verdict_allowed(size=1.0)
    opp_gate = {"opponent_holder": "redeye", "opponent_conf": 0.5,
                "opponent_opposes": False}
    final, influence = _compose_size_with_opponent(verdict, opp_gate, "BUY", "AAPL", EQUITY)
    assert influence == 0.0
    assert final == pytest.approx(1.0)


def test_compose_size_pulls_down_when_opponent_opposes():
    verdict = _verdict_allowed(size=1.0)
    opp_gate = {"opponent_holder": "redeye", "opponent_conf": 0.8,
                "opponent_opposes": True}
    final, influence = _compose_size_with_opponent(verdict, opp_gate, "BUY", "AAPL", EQUITY)
    # Pull = conf × opponent_influence, clamped to max_single_agent_influence.
    expected_pull = min(0.8 * EQUITY["OPPONENT_INFLUENCE"], EQUITY["MAX_SINGLE_AGENT_INFLUENCE"])
    assert influence == pytest.approx(expected_pull)
    assert final < 1.0
    # Reason should reflect the influence applied.
    assert "opposes" in opp_gate["reason"]
    assert opp_gate["opp_influence_applied"] == pytest.approx(expected_pull)


def test_compose_size_zero_when_verdict_blocked():
    verdict = _verdict_blocked()
    opp_gate = {"opponent_holder": "redeye", "opponent_conf": 0.8,
                "opponent_opposes": True}
    final, influence = _compose_size_with_opponent(verdict, opp_gate, "BUY", "AAPL", EQUITY)
    assert final == 0.0
    assert influence == 0.0


def test_compose_size_respects_max_single_agent_influence_cap():
    verdict = _verdict_allowed(size=1.0)
    opp_gate = {"opponent_holder": "redeye", "opponent_conf": 1.0,
                "opponent_opposes": True}
    final, influence = _compose_size_with_opponent(verdict, opp_gate, "BUY", "AAPL", EQUITY)
    # influence is capped by MAX_SINGLE_AGENT_INFLUENCE regardless of inputs.
    assert influence <= EQUITY["MAX_SINGLE_AGENT_INFLUENCE"] + 1e-9
    assert final >= EQUITY["MAX_DOWNWEIGHT"] - 1e-9


# ─────────────────── _build_governor_gate ───────────────────

def test_build_governor_gate_shape_for_pass():
    verdict = _governance_verdict(
        {"confidence": 0.7, "symbol": "AAPL", "action": "BUY"},
        gov_norm={"veto": False, "executable": True, "confidence": 0.6, "stance": "ENDORSE"},
        governor_alive=True, governor_holder="chevelle", policy=EQUITY,
    )
    row = _build_governor_gate(
        verdict, governor_holder="chevelle", executor_holder="alpha",
        gov_norm={"confidence": 0.6, "stance": "ENDORSE"},
        intent={"confidence": 0.7}, policy=EQUITY, lane="equity",
        sym="AAPL", gov_any_ts="2026-01-01T00:00:00+00:00",
    )
    assert row["name"] == "governor_authority"
    assert row["passed"] is True
    assert row["verdict_code"] == "NO_GOVERNOR_DISSENT"
    assert row["disagreement"] is False
    assert row["policy_used"] == "equity"
    assert "size×" in row["reason"]


def test_build_governor_gate_shape_for_seat_vacant():
    verdict = _governance_verdict(
        {"confidence": 0.7, "symbol": "AAPL", "action": "BUY"},
        gov_norm=None, governor_alive=True, governor_holder=None, policy=EQUITY,
    )
    row = _build_governor_gate(
        verdict, governor_holder=None, executor_holder=None,
        gov_norm=None, intent={"confidence": 0.7}, policy=EQUITY,
        lane="equity", sym="AAPL", gov_any_ts=None,
    )
    assert row["passed"] is False
    assert row["verdict_code"] == "GOVERNOR_SEAT_VACANT"
    assert "vacant" in row["reason"].lower()


# ─────────────────── _quantum_opinions ───────────────────

def test_quantum_opinions_executor_only_when_governor_silent():
    intent = {"confidence": 0.7, "stack": "alpha"}
    ops = _quantum_opinions(
        intent, action="BUY", executor_holder="alpha",
        gov_norm=None, governor_holder="chevelle",
        opp_gate={"opponent_side": None, "opponent_holder": None, "opponent_conf": 0.0},
    )
    assert len(ops) == 1
    assert ops[0].brain == "alpha"
    assert ops[0].direction == "BUY"


def test_quantum_opinions_governor_dissent_maps_to_hold():
    intent = {"confidence": 0.7}
    ops = _quantum_opinions(
        intent, action="BUY", executor_holder="alpha",
        gov_norm={"veto": True, "executable": False, "confidence": 0.9, "stance": "VETO"},
        governor_holder="chevelle",
        opp_gate={"opponent_side": None, "opponent_holder": None, "opponent_conf": 0.0},
    )
    assert len(ops) == 2
    # Governor opinion is the second; veto/dissent → HOLD direction.
    assert ops[1].brain == "chevelle"
    assert ops[1].direction == "HOLD"


def test_quantum_opinions_opponent_bearish_maps_to_short():
    intent = {"confidence": 0.7}
    ops = _quantum_opinions(
        intent, action="BUY", executor_holder="alpha",
        gov_norm=None, governor_holder=None,
        opp_gate={"opponent_side": "bearish", "opponent_holder": "redeye", "opponent_conf": 0.5},
    )
    assert len(ops) == 2  # executor + opponent (no governor)
    assert ops[1].brain == "redeye"
    assert ops[1].direction == "SHORT"
