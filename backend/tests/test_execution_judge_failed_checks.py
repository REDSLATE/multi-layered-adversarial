"""Tests for the executor-judge `failed_checks` + `not_ready_reason`
surfacing (2026-05-31).

Doctrine pin: when the execution_judge returns `execution_ready=False`,
it MUST name WHICH checks failed so the operator can tell apart:
  - missing snapshot fields (brain fix)
  - quality below threshold (different brain fix)
  - lane condition (e.g. wide spread — wait or change strategy)
  - infrastructure (e.g. broker disconnected)

Previously the judge collapsed all failures to one phrase ("not ready")
and the per-check dict was hidden behind `all(execution_checks.values())`.
This made every blocked intent look identical on the wire.
"""
from __future__ import annotations

import pytest

from shared.crypto.doctrine.crypto_brain_sidecars import _build_execution_judge
from shared.doctrine.strategy_doctrines import _build_gap_and_go_v1, _build_micro_pullback_v1
from shared.doctrine.lane_doctrine_router import hoist_packet_audit_fields


class _Base:
    def __init__(self, quality, score, symbol="X", labels=None, reasons=None):
        self.quality = quality
        self.score = score
        self.symbol = symbol
        self.labels = labels or []
        self.reasons = reasons or []


# ─── crypto execution-judge ────────────────────────────────────────

def test_crypto_judge_lists_each_failing_check_individually():
    """All 5 crypto checks fail simultaneously → failed_checks names
    all five so the operator/brain knows exactly what to fix."""
    base = _Base(quality="REJECT", score=0.25)
    j = _build_execution_judge(
        base=base, labels=set(),  # no liquidity, no quality
        holder="redeye",
        snapshot={},  # no existing intent
    )
    assert j["execution_ready"] is False
    failed = j["failed_checks"]
    assert "has_existing_intent" in failed
    assert "liquidity_ok" in failed
    assert "quality_ok" in failed
    assert "score_ok" in failed
    # spread_ok is True when WIDE_SPREAD is absent
    assert "spread_ok" not in failed
    assert j["not_ready_reason"]
    for k in failed:
        assert k in j["not_ready_reason"]


def test_crypto_judge_ready_when_all_checks_pass_and_no_reason():
    base = _Base(quality="A_QUALITY", score=0.85)
    j = _build_execution_judge(
        base=base,
        labels={"EXCHANGE_LIQUIDITY_OK"},
        holder="alpha",
        snapshot={"existing_intent": {"side": "BUY"}},
    )
    assert j["execution_ready"] is True
    assert j["failed_checks"] == []
    assert j["not_ready_reason"] is None


def test_crypto_judge_distinguishes_missing_intent_from_low_quality():
    """The two most common 'not_ready' cases must look different on
    the wire — missing-intent is a REDEYE pipeline issue; low-quality
    is an entirely different conversation."""
    base_low_q = _Base(quality="REJECT", score=0.25)
    j_low = _build_execution_judge(
        base=base_low_q,
        labels={"EXCHANGE_LIQUIDITY_OK"},
        holder="alpha",
        snapshot={"existing_intent": {"side": "BUY"}},  # intent exists
    )
    assert j_low["execution_ready"] is False
    assert "has_existing_intent" not in j_low["failed_checks"]
    assert "quality_ok" in j_low["failed_checks"]

    base_high_q = _Base(quality="A_QUALITY", score=0.85)
    j_missing = _build_execution_judge(
        base=base_high_q,
        labels={"EXCHANGE_LIQUIDITY_OK"},
        holder="alpha",
        snapshot={},  # no intent
    )
    assert j_missing["execution_ready"] is False
    assert "has_existing_intent" in j_missing["failed_checks"]
    assert "quality_ok" not in j_missing["failed_checks"]


# ─── equity execution-judge (gap-and-go) ───────────────────────────

def test_equity_gap_and_go_judge_lists_failed_checks():
    """The 5 gap-and-go checks must each be nameable as a failure."""
    snapshot = {
        "symbol": "TSLA",
        # No STRONG_GAPPER label, no premarket, no above_emas, etc.
    }
    packet = _build_gap_and_go_v1(snapshot, seat_holders={})
    ej = packet["seats"]["execution_judge"]
    assert ej["execution_ready"] is False
    assert isinstance(ej["failed_checks"], list)
    assert len(ej["failed_checks"]) > 0
    assert ej["not_ready_reason"] is not None


def test_equity_micro_pullback_judge_lists_failed_checks():
    snapshot = {"symbol": "NVDA"}
    packet = _build_micro_pullback_v1(snapshot, seat_holders={})
    ej = packet["seats"]["execution_judge"]
    assert ej["execution_ready"] is False
    assert isinstance(ej["failed_checks"], list)
    assert ej["not_ready_reason"]


# ─── hoist plumbing — the failed_checks list must survive
#     into the audit row so the front-end and operator can read it. ─

def test_hoist_propagates_failed_checks_and_reason():
    """`hoist_packet_audit_fields` must copy the new fields into the
    flat audit dict. Otherwise the front-end gets the chip update but
    no per-row drill-down."""
    snapshot = {"symbol": "NVDA"}
    packet = _build_gap_and_go_v1(snapshot, seat_holders={})
    hoisted = hoist_packet_audit_fields(packet)
    assert "execution_judge_failed_checks" in hoisted
    assert isinstance(hoisted["execution_judge_failed_checks"], list)
    assert len(hoisted["execution_judge_failed_checks"]) > 0
    assert "execution_judge_not_ready_reason" in hoisted
    assert hoisted["execution_judge_not_ready_reason"]


def test_hoist_empty_packet_defaults_safely():
    """No packet → empty list for failed_checks, None for reason. Must
    not crash audit-row construction for legacy intents missing a packet."""
    hoisted = hoist_packet_audit_fields({})
    assert hoisted["execution_judge_failed_checks"] == []
    assert hoisted["execution_judge_not_ready_reason"] is None
