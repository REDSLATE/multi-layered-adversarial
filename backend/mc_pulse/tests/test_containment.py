"""Containment: a broken brain must never silence the others."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from mc_arbiter.models import Direction, ModelOpinion
from mc_arbiter.seat_key import build_seat_key
from mc_pulse.containment import evaluate_brain
from mc_pulse.snapshot import build_snapshot


def _snap(symbol="NVDA"):
    return build_snapshot(
        symbol=symbol, lane="equity",
        timestamp=datetime(2026, 7, 11, 14, 30, tzinfo=timezone.utc),
        price=Decimal("140.50"),
        indicators={"atr": 1.2},
    )


class _GoodBrain:
    id = "good"
    lanes = frozenset({"equity"})
    cadence_seconds = 30
    evaluation_timeout_seconds = 2.0

    def should_evaluate(self, *, now, snapshot):
        return True

    async def evaluate(self, snapshot):
        return ModelOpinion(
            brain=self.id, seat_key="unused",
            direction=Direction.LONG,
            edge=0.5, confidence=0.6, regime_fit=0.7, urgency=0.5,
            price_at_signal=float(snapshot.price),
            ts=snapshot.timestamp.isoformat(),
        )


class _SlowBrain:
    id = "slow"
    lanes = frozenset({"equity"})
    cadence_seconds = 30
    evaluation_timeout_seconds = 0.10   # tight — will timeout

    def should_evaluate(self, *, now, snapshot):
        return True

    async def evaluate(self, snapshot):
        await asyncio.sleep(5.0)  # exceeds timeout
        return None


class _RaisingBrain:
    id = "raising"
    lanes = frozenset({"equity"})
    cadence_seconds = 30
    evaluation_timeout_seconds = 2.0

    def should_evaluate(self, *, now, snapshot):
        return True

    async def evaluate(self, snapshot):
        raise RuntimeError("boom")


class _SilentBrain:
    id = "silent"
    lanes = frozenset({"equity"})
    cadence_seconds = 30
    evaluation_timeout_seconds = 2.0

    def should_evaluate(self, *, now, snapshot):
        return True

    async def evaluate(self, snapshot):
        return None  # "I looked and had nothing to say"


@pytest.mark.asyncio
async def test_good_brain_returns_envelope():
    snap = _snap()
    seat = build_seat_key("equity", snap.symbol, snap.timestamp)
    env, fail = await evaluate_brain(_GoodBrain(), snap, pulse_id="p1", seat_key=seat)
    assert fail is None
    assert env is not None
    assert env.pulse_id == "p1"
    assert env.brain_id == "good"
    assert env.snapshot_id == snap.snapshot_id
    assert env.opinion.direction == Direction.LONG


@pytest.mark.asyncio
async def test_slow_brain_times_out_cleanly():
    snap = _snap()
    seat = build_seat_key("equity", snap.symbol, snap.timestamp)
    env, fail = await evaluate_brain(_SlowBrain(), snap, pulse_id="p1", seat_key=seat)
    assert env is None
    assert fail is not None
    assert fail.brain_id == "slow"
    assert fail.reason == "evaluation_timeout"
    assert fail.exc_type is None


@pytest.mark.asyncio
async def test_raising_brain_records_failure_and_does_not_raise():
    snap = _snap()
    seat = build_seat_key("equity", snap.symbol, snap.timestamp)
    # No pytest.raises — the point of containment is NOTHING raises
    env, fail = await evaluate_brain(_RaisingBrain(), snap, pulse_id="p1", seat_key=seat)
    assert env is None
    assert fail is not None
    assert fail.brain_id == "raising"
    assert fail.reason == "evaluation_error"
    assert fail.exc_type == "RuntimeError"


@pytest.mark.asyncio
async def test_silent_brain_returns_no_envelope_no_failure():
    snap = _snap()
    seat = build_seat_key("equity", snap.symbol, snap.timestamp)
    env, fail = await evaluate_brain(_SilentBrain(), snap, pulse_id="p1", seat_key=seat)
    # None + None = "brain completed cleanly, just no opinion".
    # NOT a failure — the pulse receipt counts this brain as
    # `completed`, not `failed`.
    assert env is None
    assert fail is None


@pytest.mark.asyncio
async def test_gather_of_mixed_brains_survives_failures():
    """The concrete pulse doctrine: one broken brain MUST NOT
    silence the others. Simulate the exact fanout the pulse does."""
    snap = _snap()
    seat = build_seat_key("equity", snap.symbol, snap.timestamp)
    tasks = [
        evaluate_brain(_GoodBrain(), snap, pulse_id="p1", seat_key=seat),
        evaluate_brain(_RaisingBrain(), snap, pulse_id="p1", seat_key=seat),
        evaluate_brain(_SlowBrain(), snap, pulse_id="p1", seat_key=seat),
        evaluate_brain(_SilentBrain(), snap, pulse_id="p1", seat_key=seat),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    envelopes = [r[0] for r in results if r[0] is not None]
    failures = [r[1] for r in results if r[1] is not None]
    # Good brain landed an envelope; silent completed without one;
    # slow + raising each produced a failure receipt.
    assert len(envelopes) == 1
    assert envelopes[0].brain_id == "good"
    assert {f.brain_id for f in failures} == {"slow", "raising"}
