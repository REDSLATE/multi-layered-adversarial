"""Unified execution pipeline — branch coverage.

Tests each branch of `run_execution_pipeline` in isolation with fake
seat/governor/roadguard/broker/receipt-store. The point is to lock
the contract:
  * Brain HOLD → NO_ORDER (source=brain)
  * Seat BLOCK → BLOCKED (source=seat)
  * RoadGuard fails → BLOCKED (source=roadguard)
  * observe/shadow → DECISION_LOGGED (source=seat), no broker call
  * toehold → broker called with clamped notional
  * auto_execute → broker called with computed notional
  * broker raises → BROKER_ERROR (source=broker)
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any, Dict, List

import pytest

from shared.pipeline.models import (
    BrainOpinion,
    GovernorModifier,
    PipelineReceipt,
    RoadGuardVerdict,
    SeatVerdict,
)
from shared.pipeline.execution_pipeline import run_execution_pipeline


# ── Test doubles ──────────────────────────────────────────────────────


class _FakeSeat:
    def __init__(self, verdict: SeatVerdict) -> None:
        self.verdict = verdict
        self.calls = 0

    async def evaluate(self, opinion: BrainOpinion) -> SeatVerdict:
        self.calls += 1
        return self.verdict


class _FakeGovernor:
    def __init__(self, mult: float = 1.0) -> None:
        self.mult = mult

    async def modify(self, opinion: BrainOpinion) -> GovernorModifier:
        return GovernorModifier(risk_multiplier=self.mult, reason="fake")


class _FakeRoadGuard:
    def __init__(self, passed: bool = True, reason: str = "ok") -> None:
        self.passed = passed
        self.reason = reason

    async def check(self, opinion: BrainOpinion, notional: float) -> RoadGuardVerdict:
        return RoadGuardVerdict(passed=self.passed, reason=self.reason)


class _FakeBroker:
    def __init__(self, raise_exc: BaseException | None = None) -> None:
        self.raise_exc = raise_exc
        self.calls: List[Dict[str, Any]] = []

    async def submit_market_order(self, *, symbol: str, side: str,
                                  notional_usd: float, lane: str) -> Dict[str, Any]:
        self.calls.append({
            "symbol": symbol, "side": side,
            "notional_usd": notional_usd, "lane": lane,
        })
        if self.raise_exc:
            raise self.raise_exc
        return {"status": "submitted"}


class _FakeReceiptStore:
    def __init__(self) -> None:
        self.written: List[PipelineReceipt] = []

    async def write(self, receipt: PipelineReceipt) -> None:
        self.written.append(receipt)


def _opinion(**over: Any) -> BrainOpinion:
    base = dict(
        intent_id="intent-1", brain_id="camaro", lane="equity",
        symbol="AAPL", action="BUY", confidence=0.85,
        notional_usd=100.0, evidence={"market_open": True},
    )
    base.update(over)
    return BrainOpinion(**base)


async def _run(opinion: BrainOpinion, *, seat=None, gov=None, rg=None, broker=None):
    store = _FakeReceiptStore()
    receipt = await run_execution_pipeline(
        opinion,
        seat_policy=seat or _FakeSeat(SeatVerdict("ALLOW", "ok", "auto_execute", 50.0)),
        governor=gov or _FakeGovernor(1.0),
        roadguard=rg or _FakeRoadGuard(True),
        broker=broker or _FakeBroker(),
        receipt_store=store,
    )
    return receipt, store


# ── Test cases ────────────────────────────────────────────────────────


def test_brain_hold_yields_no_order():
    receipt, store = asyncio.run(_run(_opinion(action="HOLD")))
    assert receipt.final_status == "NO_ORDER"
    assert receipt.restriction_source == "brain"
    assert receipt.broker_called is False
    assert receipt.final_notional == 0.0
    assert len(store.written) == 1


def test_brain_abstain_yields_no_order():
    receipt, _ = asyncio.run(_run(_opinion(action="ABSTAIN")))
    assert receipt.final_status == "NO_ORDER"
    assert receipt.restriction_source == "brain"


def test_seat_block_yields_blocked_receipt():
    seat = _FakeSeat(SeatVerdict("BLOCK", "seat_disabled", "observe", 0.0))
    broker = _FakeBroker()
    receipt, _ = asyncio.run(_run(_opinion(), seat=seat, broker=broker))
    assert receipt.final_status == "BLOCKED"
    assert receipt.restriction_source == "seat"
    assert receipt.final_reason == "seat_disabled"
    assert broker.calls == []


def test_roadguard_block_yields_blocked_receipt():
    rg = _FakeRoadGuard(passed=False, reason="market_closed")
    broker = _FakeBroker()
    receipt, _ = asyncio.run(_run(_opinion(), rg=rg, broker=broker))
    assert receipt.final_status == "BLOCKED"
    assert receipt.restriction_source == "roadguard"
    assert receipt.final_reason == "market_closed"
    assert broker.calls == []


def test_observe_mode_logs_decision_no_broker():
    seat = _FakeSeat(SeatVerdict("ALLOW", "ok", "observe", 50.0))
    broker = _FakeBroker()
    receipt, _ = asyncio.run(_run(_opinion(), seat=seat, broker=broker))
    assert receipt.final_status == "DECISION_LOGGED"
    assert receipt.restriction_source == "seat"
    assert receipt.broker_called is False
    assert broker.calls == []
    # The final_notional MUST be > 0 so the verifier can grade what
    # the seat *would* have sized to.
    assert receipt.final_notional > 0


def test_shadow_mode_logs_decision_no_broker():
    seat = _FakeSeat(SeatVerdict("ALLOW", "ok", "shadow", 50.0))
    broker = _FakeBroker()
    receipt, _ = asyncio.run(_run(_opinion(), seat=seat, broker=broker))
    assert receipt.final_status == "DECISION_LOGGED"
    assert receipt.restriction_source == "seat"
    assert broker.calls == []


def test_toehold_clamps_notional_at_seat_cap():
    # seat cap = $25; request = $100 → final clamped at $25.
    seat = _FakeSeat(SeatVerdict("ALLOW", "ok", "toehold", 25.0))
    broker = _FakeBroker()
    receipt, _ = asyncio.run(_run(_opinion(notional_usd=100.0), seat=seat, broker=broker))
    assert receipt.final_status == "SUBMITTED"
    assert receipt.restriction_source == "broker"
    assert receipt.broker_called is True
    assert broker.calls[0]["notional_usd"] == 25.0


def test_auto_execute_uses_governor_multiplier():
    seat = _FakeSeat(SeatVerdict("ALLOW", "ok", "auto_execute", 100.0))
    gov = _FakeGovernor(0.5)  # cut sizing in half
    broker = _FakeBroker()
    receipt, _ = asyncio.run(_run(_opinion(notional_usd=80.0), seat=seat, gov=gov, broker=broker))
    assert receipt.final_status == "SUBMITTED"
    assert receipt.broker_called is True
    # min(seat=100, req=80) * 0.5 = 40
    assert receipt.final_notional == 40.0
    assert broker.calls[0]["notional_usd"] == 40.0


def test_broker_exception_yields_broker_error_receipt():
    broker = _FakeBroker(raise_exc=RuntimeError("connection refused"))
    receipt, _ = asyncio.run(_run(_opinion(), broker=broker))
    assert receipt.final_status == "BROKER_ERROR"
    assert receipt.restriction_source == "broker"
    assert "connection refused" in receipt.final_reason
    # broker_called must be True even on failure — the call was attempted.
    assert receipt.broker_called is True


def test_every_path_writes_exactly_one_receipt():
    """The whole-system invariant: one intent, one receipt."""
    cases = [
        ("hold",    _opinion(action="HOLD"),  None,                                                None),
        ("seat",    _opinion(),               _FakeSeat(SeatVerdict("BLOCK", "x", "observe", 0)),  None),
        ("rg",      _opinion(),               None,                                                _FakeRoadGuard(False, "x")),
        ("obs",     _opinion(),               _FakeSeat(SeatVerdict("ALLOW","ok","observe",50)),   None),
        ("execute", _opinion(),               None,                                                None),
    ]
    for label, opinion, seat, rg in cases:
        _, store = asyncio.run(_run(opinion, seat=seat, rg=rg))
        assert len(store.written) == 1, f"{label}: expected 1 receipt, got {len(store.written)}"


def test_governor_cannot_block_via_zero_multiplier():
    """The doctrine pin: governor CANNOT collapse an order to $0.
    Its multiplier is clamped to [0.05, 1.0] by the real Governor.
    Even if upstream evidence asks for 0, the floor keeps the order
    alive — block authority still lives with seat/roadguard/broker."""
    from shared.pipeline.governor import Governor as RealGovernor
    seat = _FakeSeat(SeatVerdict("ALLOW", "ok", "auto_execute", 100.0))
    broker = _FakeBroker()
    # Evidence tries to collapse risk to 0; real Governor clamps to 0.05.
    opinion = _opinion(notional_usd=100.0, evidence={"market_open": True, "risk_multiplier": 0.0})
    receipt, _ = asyncio.run(_run(opinion, seat=seat, gov=RealGovernor(), broker=broker))
    assert receipt.final_status == "SUBMITTED"
    assert receipt.restriction_source == "broker"
    # min(seat=100, req=100) * clamped(0.05) = 5.0
    assert receipt.final_notional == 5.0
    assert receipt.governor_multiplier == 0.05
