"""Pulse health metric contract tests.

2026-07-12 (P4): replaces the retired `test_parity_snapshotter`.
The pulse-health schema is durable — it doesn't reference the
runner, doesn't compute match_score, and doesn't gate on any
migration criterion. Metrics measure the four brains as
independently as possible so that P7 (strategy split) has a
before/after baseline.
"""
from __future__ import annotations

import pytest


# ─────────────── snapshot persistence ───────────────

@pytest.mark.asyncio
async def test_snapshot_row_has_full_pulse_health_schema(monkeypatch):
    from mc_pulse import pulse_health_routes as ph

    async def _fake_compute(brain_id, *, hours):
        return {
            "brain": brain_id,
            "evaluation_count": 300,
            "action_distribution": {
                "counts": {"LONG": 70, "SHORT": 30, "FLAT": 200},
                "pct": {"LONG": 23.3, "SHORT": 10.0, "FLAT": 66.7},
            },
            "confidence_mean": 0.61,
            "confidence_std": 0.20,
            "stale_input_rate": 0.05,
            "no_data_rate": 0.02,
            "no_data_breakdown": {
                "cadence_cooldown": {"count": 4, "percent": 1.3},
                "snapshot_stale": {"count": 2, "percent": 0.7},
            },
            "exception_rate": 0.00,
            "duplicate_opinion_rate": 0.03,
            "latest_source_bar_at": "2026-07-12T15:00:00+00:00",
            "pulse_lag_ms": 220.0,
            "distinctness": {
                "pairwise_agreement_rate": 0.42,
                "distinctness": 0.58,
                "peer_matches": 180,
            },
        }

    async def _fake_insert(doc):
        return type("R", (), {"inserted_id": "fake"})()

    monkeypatch.setattr(ph, "compute_pulse_health", _fake_compute)

    class _FakeCol:
        insert_one = staticmethod(_fake_insert)

    class _FakeDB:
        def __getitem__(self, name):
            return _FakeCol()

    monkeypatch.setattr(ph, "db", _FakeDB())

    doc = await ph.take_pulse_health_snapshot("camino", hours=24)

    # Every field the operator specified in the P4 schema is present.
    for k in (
        "at", "brain", "window_hours", "evaluation_count",
        "action_distribution", "confidence_mean", "confidence_std",
        "stale_input_rate", "no_data_rate", "no_data_breakdown",
        "exception_rate",
        "duplicate_opinion_rate", "latest_source_bar_at", "pulse_lag_ms",
        "distinctness",
    ):
        assert k in doc, f"missing pulse-health field: {k}"

    # Every retired parity field is ABSENT — this test locks the
    # rename against a silent regression that adds them back.
    for retired in (
        "match_score", "runner_count", "pairs_matched",
        "timestamp_drift_median_s", "arbiter_flip_gates_pass",
        "gates", "rationale_jaccard_mean", "pulse_count",
    ):
        assert retired not in doc, (
            f"retired parity field {retired!r} leaked back into "
            "pulse-health snapshot — reject the drift"
        )


@pytest.mark.asyncio
async def test_snapshot_failsoft_on_compute_error(monkeypatch):
    from mc_pulse import pulse_health_routes as ph

    async def _blow_up(*a, **kw):
        raise RuntimeError("atlas timeout")

    monkeypatch.setattr(ph, "compute_pulse_health", _blow_up)
    doc = await ph.take_pulse_health_snapshot("camino", hours=24)
    assert doc == {}


# ─────────────── metric primitives ───────────────

def _row(brain="camino", direction="LONG", conf=0.7, sym="AAPL",
         bc="2026-07-12T15:00:00+00:00", status="OK", pulse_id="p1",
         evaluated_at="2026-07-12T15:00:30+00:00"):
    """Emulate a `mc_opinions_compare` row — matches the actual
    schema: top-level `direction`/`confidence`/`status`, plus
    `bucket_iso` as the canonical bar key."""
    return {
        "brain": brain,
        "symbol": sym,
        "pulse_id": pulse_id,
        "evaluated_at": evaluated_at,
        "direction": direction,
        "confidence": conf,
        "status": status,
        "bucket_iso": bc,
    }


def test_action_distribution_bins_correctly():
    from mc_pulse.pulse_health_routes import _action_distribution
    rows = [_row(direction="LONG"), _row(direction="LONG"),
            _row(direction="SHORT"), _row(direction="FLAT")]
    d = _action_distribution(rows)
    assert d["counts"] == {"LONG": 2, "SHORT": 1, "FLAT": 1}
    assert d["pct"]["LONG"] == 50.0
    assert d["pct"]["SHORT"] == 25.0
    assert d["pct"]["FLAT"] == 25.0


def test_confidence_stats():
    from mc_pulse.pulse_health_routes import (
        _confidence_mean, _confidence_std,
    )
    rows = [_row(conf=0.5), _row(conf=0.7), _row(conf=0.9)]
    assert _confidence_mean(rows) == 0.7
    assert _confidence_std(rows) > 0.15


def test_stale_input_rate_counts_insufficient_data():
    from mc_pulse.pulse_health_routes import _stale_input_rate
    rows = [_row(status="OK"), _row(status="INSUFFICIENT_DATA"),
            _row(status="OK"), _row(status="INSUFFICIENT_DATA")]
    assert _stale_input_rate(rows) == 0.5


def test_no_data_rate_when_brain_silent_across_all_pulses():
    from mc_pulse.pulse_health_routes import _no_data_rate
    pulses = [{"pulse_id": "p1"}, {"pulse_id": "p2"}, {"pulse_id": "p3"}]
    opinions = []  # brain contributed to nothing
    assert _no_data_rate(opinions, pulses, "camino") == 1.0

    # brain contributed to 1 of 3 pulses
    opinions = [_row(pulse_id="p1")]
    assert _no_data_rate(opinions, pulses, "camino") == round(2/3, 4)


def test_no_data_breakdown_categorises_silence_by_reason():
    """P1 (2026-02-11): silence rows on the pulse receipt get
    aggregated into a per-reason percentage table so the operator
    can see WHY the brain didn't think, not just that it didn't."""
    from mc_pulse.pulse_health_routes import _no_data_breakdown
    pulses = [
        # Pulse 1: camino contributed → not silent.
        {"pulse_id": "p1", "brains_expected": 4,
         "brains_completed": ["camino", "gto"],
         "brains_silent": [
             {"brain_id": "barracuda", "reason": "snapshot_stale"},
             {"brain_id": "hellcat", "reason": "cadence_cooldown"},
         ]},
        # Pulse 2: camino silent — snapshot_stale.
        {"pulse_id": "p2", "brains_expected": 4,
         "brains_completed": ["gto"],
         "brains_silent": [
             {"brain_id": "camino", "reason": "snapshot_stale"},
             {"brain_id": "barracuda", "reason": "snapshot_stale"},
             {"brain_id": "hellcat", "reason": "snapshot_stale"},
         ]},
        # Pulse 3: camino silent — cadence_cooldown.
        {"pulse_id": "p3", "brains_expected": 4,
         "brains_completed": ["gto"],
         "brains_silent": [
             {"brain_id": "camino", "reason": "cadence_cooldown"},
         ]},
        # Pulse 4: camino silent — no_signal_return.
        {"pulse_id": "p4", "brains_expected": 4,
         "brains_completed": ["camino", "gto"],
         "brains_silent": [
             {"brain_id": "camino", "reason": "no_signal_return"},
         ]},
    ]
    # NB: camino appears in brains_completed for p4, so p4 counts
    # as a contribution — the no_signal_return silence should be
    # ignored for camino on that pulse.
    br = _no_data_breakdown(pulses, "camino")
    assert br["snapshot_stale"]["count"] == 1
    assert br["cadence_cooldown"]["count"] == 1
    assert "no_signal_return" not in br  # contributed on p4
    # Percentages are of TOTAL pulses (4), not of silent pulses (2).
    assert br["snapshot_stale"]["percent"] == 25.0
    assert br["cadence_cooldown"]["percent"] == 25.0


def test_no_data_breakdown_stamps_unknown_for_pre_p1_pulses():
    """Pulses that predate the BrainSilence stamp (no
    `brains_silent` field) still show up in `no_data_rate` as
    silent pulses — attribute them to `unknown` so the
    percentages add up. Decays within one window as fresh pulses
    replace stale ones."""
    from mc_pulse.pulse_health_routes import _no_data_breakdown
    pulses = [
        # Pre-P1 pulse: no brains_silent field, brain didn't contribute.
        {"pulse_id": "p_old", "brains_expected": 4,
         "brains_completed": ["gto"]},
        # Fresh P1 pulse: stamped correctly.
        {"pulse_id": "p_new", "brains_expected": 4,
         "brains_completed": ["gto"],
         "brains_silent": [
             {"brain_id": "camino", "reason": "snapshot_stale"},
         ]},
    ]
    br = _no_data_breakdown(pulses, "camino")
    assert br["unknown"]["count"] == 1
    assert br["snapshot_stale"]["count"] == 1


def test_no_data_breakdown_ignores_pulses_with_no_expected_brains():
    """A pulse where `brains_expected=0` had no work for anyone —
    it shouldn't inflate the unknown bucket."""
    from mc_pulse.pulse_health_routes import _no_data_breakdown
    pulses = [
        {"pulse_id": "p_empty", "brains_expected": 0,
         "brains_completed": []},
    ]
    br = _no_data_breakdown(pulses, "camino")
    assert br == {}


def test_exception_rate_counts_containment_failures():
    from mc_pulse.pulse_health_routes import _exception_rate
    pulses = [
        {"pulse_id": "p1", "brains_failed": []},
        {"pulse_id": "p2", "brains_failed": [
            {"brain": "camino", "reason": "timeout", "exc_type": "TimeoutError"},
        ]},
        {"pulse_id": "p3", "brains_failed": [
            {"brain": "gto", "reason": "value", "exc_type": "ValueError"},
        ]},
    ]
    assert _exception_rate(pulses, "camino") == round(1/3, 4)
    assert _exception_rate(pulses, "gto") == round(1/3, 4)
    assert _exception_rate(pulses, "barracuda") == 0.0


def test_duplicate_opinion_rate():
    """Two rows with the same (symbol, bar_close, direction) → 50% dup."""
    from mc_pulse.pulse_health_routes import _duplicate_opinion_rate
    rows = [
        _row(sym="NVDA", bc="2026-07-12T15:00:00+00:00", direction="LONG"),
        _row(sym="NVDA", bc="2026-07-12T15:00:00+00:00", direction="LONG"),
    ]
    assert _duplicate_opinion_rate(rows) == 0.5


def test_distinctness_full_agreement_is_zero():
    """4 brains all LONG on the same (symbol, bar_close) → distinctness=0.
    The operator explicitly said: honest agreement is fine, distinctness
    is a health signal, not a flip gate."""
    from mc_pulse.pulse_health_routes import _distinctness
    self_rows = [_row(brain="camino", direction="LONG")]
    peer_rows = [
        _row(brain="gto",       direction="LONG"),
        _row(brain="barracuda", direction="LONG"),
        _row(brain="hellcat",   direction="LONG"),
    ]
    d = _distinctness(self_rows, peer_rows)
    assert d["pairwise_agreement_rate"] == 1.0
    assert d["distinctness"] == 0.0
    assert d["peer_matches"] == 3


def test_distinctness_full_disagreement_is_one():
    from mc_pulse.pulse_health_routes import _distinctness
    self_rows = [_row(brain="camino", direction="LONG")]
    peer_rows = [
        _row(brain="gto",       direction="SHORT"),
        _row(brain="barracuda", direction="SHORT"),
        _row(brain="hellcat",   direction="FLAT"),
    ]
    d = _distinctness(self_rows, peer_rows)
    assert d["pairwise_agreement_rate"] == 0.0
    assert d["distinctness"] == 1.0


def test_distinctness_no_overlap_returns_null():
    """No shared (symbol, bar_close) between self and peers → null."""
    from mc_pulse.pulse_health_routes import _distinctness
    self_rows = [_row(brain="camino", sym="AAPL")]
    peer_rows = [_row(brain="gto", sym="NVDA")]
    d = _distinctness(self_rows, peer_rows)
    assert d["distinctness"] is None
    assert d["peer_matches"] == 0
