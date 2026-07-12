"""Parity snapshotter contract tests.

2026-07-12 doctrine: the pulse-vs-runner parity trend must be
observable without repeated manual polling. `take_parity_snapshot`
writes ONE compact row to `mc_parity_snapshots` per call.
`arbiter_flip_gates_pass` is True iff MC_PULSE.md §11 gates all
hold: match_score ≥ 0.60, pulse conf std > 0.02,
pairs_matched ≥ 20.
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_snapshot_persists_compact_row(monkeypatch):
    """Snapshot writes exactly one row with the trend fields."""
    from mc_pulse import parity_routes as pr

    captured = {}

    async def _fake_compute(brain_id, *, hours, sample_size):
        return {
            "brain": brain_id.lower(),
            "window_hours": hours,
            "since": "2026-07-12T00:00:00+00:00",
            "pulse_count": 1000,
            "runner_count": 50,
            "action_distribution": {"match_score": 0.72},
            "confidence_distribution": {
                "pulse": {"mean": 0.61, "std": 0.20},
                "runner": {"mean": 0.61, "std": 0.13},
            },
            "rationale_token_overlap": {"jaccard_mean": 0.055},
            "timestamp_drift_s": {"median_s": 97.3, "pairs_matched": 161},
            "samples": [],
        }

    async def _fake_insert(doc):
        captured.update(doc)
        return type("R", (), {"inserted_id": "fake"})()

    monkeypatch.setattr(pr, "compute_parity", _fake_compute)

    class _FakeCol:
        insert_one = staticmethod(_fake_insert)

    class _FakeDB:
        def __getitem__(self, name):
            return _FakeCol()

    monkeypatch.setattr(pr, "db", _FakeDB())

    doc = await pr.take_parity_snapshot("camino", hours=24)
    assert doc["brain"] == "camino"
    assert doc["match_score"] == 0.72
    assert doc["pulse_confidence_std"] == 0.20
    assert doc["pairs_matched"] == 161
    assert doc["arbiter_flip_gates_pass"] is True
    assert doc["gates"] == {
        "match_score_ok": True,
        "conf_std_ok": True,
        "pairs_matched_ok": True,
    }


@pytest.mark.asyncio
async def test_gates_reject_below_threshold(monkeypatch):
    """Under-threshold metrics MUST set arbiter_flip_gates_pass=False."""
    from mc_pulse import parity_routes as pr

    async def _fake_compute(brain_id, *, hours, sample_size):
        return {
            "brain": brain_id,
            "pulse_count": 10, "runner_count": 5,
            "action_distribution": {"match_score": 0.55},  # < 0.60
            "confidence_distribution": {
                "pulse": {"mean": 0.5, "std": 0.01},  # ≤ 0.02
                "runner": {"mean": 0.5, "std": 0.10},
            },
            "rationale_token_overlap": {"jaccard_mean": 0.0},
            "timestamp_drift_s": {"median_s": 60.0, "pairs_matched": 5},  # < 20
        }

    async def _fake_insert(doc):
        return type("R", (), {"inserted_id": "fake"})()

    monkeypatch.setattr(pr, "compute_parity", _fake_compute)

    class _FakeCol:
        insert_one = staticmethod(_fake_insert)

    class _FakeDB:
        def __getitem__(self, name):
            return _FakeCol()

    monkeypatch.setattr(pr, "db", _FakeDB())

    doc = await pr.take_parity_snapshot("camino", hours=24)
    assert doc["arbiter_flip_gates_pass"] is False
    assert doc["gates"] == {
        "match_score_ok": False,
        "conf_std_ok": False,
        "pairs_matched_ok": False,
    }


@pytest.mark.asyncio
async def test_snapshot_failsoft_on_compute_error(monkeypatch):
    """Any exception in compute_parity → empty dict returned, no crash."""
    from mc_pulse import parity_routes as pr

    async def _blow_up(*a, **kw):
        raise RuntimeError("atlas timeout")

    monkeypatch.setattr(pr, "compute_parity", _blow_up)
    doc = await pr.take_parity_snapshot("camino", hours=24)
    assert doc == {}
