"""PulseReceipt shape + orchestration_ok honesty."""
from __future__ import annotations

from mc_pulse.receipt import BrainFailure, PulseReceipt


def _receipt(**overrides):
    base = dict(
        pulse_id="p1",
        started_at="2026-07-11T14:30:00+00:00",
        cadence_seconds=15,
    )
    base.update(overrides)
    return PulseReceipt(**base)


def test_incomplete_pulse_is_not_ok():
    # `completed_at` is None → the pulse never finished.
    # `orchestration_ok` must NOT be True.
    r = _receipt()
    assert r.orchestration_ok is False


def test_completed_pulse_with_no_failures_is_ok():
    r = _receipt(completed_at="2026-07-11T14:30:01+00:00")
    assert r.orchestration_ok is True


def test_completed_pulse_with_failure_is_not_ok():
    """The core anti-3-clock-dishonesty invariant: a green
    orchestration cannot conceal a dead brain."""
    r = _receipt(
        completed_at="2026-07-11T14:30:01+00:00",
        brains_failed=[
            BrainFailure(
                brain_id="hellcat", reason="evaluation_timeout",
                at="2026-07-11T14:30:00+00:00",
            ),
        ],
    )
    assert r.orchestration_ok is False


def test_overrun_flips_orchestration_ok():
    r = _receipt(
        completed_at="2026-07-11T14:30:01+00:00",
        overrun=True,
    )
    assert r.orchestration_ok is False


def test_to_mongo_includes_orchestration_ok():
    r = _receipt(completed_at="2026-07-11T14:30:01+00:00")
    doc = r.to_mongo()
    assert doc["_id"] == "p1"
    assert doc["orchestration_ok"] is True
    assert doc["pulse_id"] == "p1"
    # brains_failed serializes as list of dicts (not dataclass).
    assert doc["brains_failed"] == []


def test_to_mongo_serializes_brain_failures():
    r = _receipt(
        completed_at="2026-07-11T14:30:01+00:00",
        brains_failed=[
            BrainFailure(
                brain_id="hellcat", reason="evaluation_error",
                exc_type="RuntimeError", symbol="NVDA", lane="equity",
                at="2026-07-11T14:30:00+00:00",
            ),
        ],
    )
    doc = r.to_mongo()
    assert len(doc["brains_failed"]) == 1
    failure = doc["brains_failed"][0]
    assert isinstance(failure, dict)
    assert failure["brain_id"] == "hellcat"
    assert failure["exc_type"] == "RuntimeError"
