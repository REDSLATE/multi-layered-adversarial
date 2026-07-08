"""Tests for the fingerprint diffing tool.

Covers:
    - Composite aggregate summation across a list of fingerprints
    - Percentile diffs (approximate weighted-mean composites)
    - Distribution diffs (exact counts / pct)
    - Top-K reason diffs (new_in_after / dropped_from_before / count_deltas)
    - Empty-window handling (both sides zero, one side zero)
    - Invalid brain / lane raises
    - Endpoint smoke path via diff_fingerprints() DB round-trip
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "/app/backend")

from db import db
from namespaces import SESSION_FINGERPRINTS
from shared.session_fingerprint import (
    _aggregate_composite,
    _diff_percentiles,
    _diff_pct_dict,
    _diff_top_reasons,
    diff_fingerprints,
)


@pytest.fixture(autouse=True)
async def _clean_state():
    await db[SESSION_FINGERPRINTS].delete_many(
        {"_id": {"$regex": "^fp-diff-test-"}}
    )
    yield
    await db[SESSION_FINGERPRINTS].delete_many(
        {"_id": {"$regex": "^fp-diff-test-"}}
    )


def _fp(
    brain: str,
    lane: str,
    window_end_ts: str,
    intent_count: int,
    execution_ready_rate: float = 0.0,
    quality_dist: dict | None = None,
    gate_pass_rates: dict | None = None,
    top_fail_reasons: list[dict] | None = None,
    confidence_percentiles: dict | None = None,
    risk_multiplier_p50: float | None = None,
    market_regime_dist: dict | None = None,
) -> dict:
    """Build a fingerprint doc with a `_id` prefix that the autouse
    fixture cleans up."""
    return {
        "_id": f"fp-diff-test-{brain}:{lane}:{window_end_ts}",
        "brain": brain,
        "lane": lane,
        "window_end_ts": window_end_ts,
        "window_start_ts": window_end_ts,  # not used by diff
        "intent_count": intent_count,
        "execution_ready_rate": execution_ready_rate,
        "gate_state_dist": {},
        "quality_dist": quality_dist or {},
        "top_labels": [],
        "top_fail_reasons": top_fail_reasons or [],
        "top_objections": [],
        "gate_pass_rates": gate_pass_rates or {},
        "confidence_percentiles": (
            confidence_percentiles or {"p10": None, "p50": None, "p90": None}
        ),
        "risk_multiplier_p50": risk_multiplier_p50,
        "rvol_percentiles": {"p10": None, "p50": None, "p90": None},
        "gap_pct_percentiles": {"p10": None, "p50": None, "p90": None},
        "market_regime_dist": market_regime_dist or {},
    }


# ─────────────────────── pure helpers ───────────────────────


def test_aggregate_composite_sums_counts():
    fps = [
        _fp("camino", "equity", "2026-07-08T12:00:00+00:00", 3,
            quality_dist={"A_QUALITY": 1, "C_QUALITY": 2}),
        _fp("camino", "equity", "2026-07-08T12:15:00+00:00", 5,
            quality_dist={"A_QUALITY": 2, "C_QUALITY": 3}),
    ]
    agg = _aggregate_composite(fps)
    assert agg["windows_used"] == 2
    assert agg["intent_count"] == 8
    assert agg["quality_dist"] == {"A_QUALITY": 3, "C_QUALITY": 5}
    # 3 A_QUALITY / 8 = 0.375, 5 C_QUALITY / 8 = 0.625
    assert agg["quality_dist_pct"]["A_QUALITY"] == 0.375
    assert agg["quality_dist_pct"]["C_QUALITY"] == 0.625


def test_aggregate_composite_execution_ready_weighted():
    """Weighted mean by intent_count, NOT unweighted average."""
    fps = [
        _fp("camino", "equity", "t1", 10, execution_ready_rate=0.5),
        _fp("camino", "equity", "t2", 90, execution_ready_rate=0.1),
    ]
    agg = _aggregate_composite(fps)
    # (10 * 0.5 + 90 * 0.1) / 100 = 14 / 100 = 0.14
    assert agg["execution_ready_rate"] == 0.14


def test_aggregate_composite_gate_pass_rates_weighted():
    fps = [
        _fp("camino", "equity", "t1", 10,
            gate_pass_rates={"has_volume": 0.5, "spread_ok": 1.0}),
        _fp("camino", "equity", "t2", 30,
            gate_pass_rates={"has_volume": 0.1, "spread_ok": 0.5}),
    ]
    agg = _aggregate_composite(fps)
    # has_volume: (10*0.5 + 30*0.1) / 40 = 8/40 = 0.2
    assert agg["gate_pass_rates"]["has_volume"] == 0.2
    # spread_ok:  (10*1.0 + 30*0.5) / 40 = 25/40 = 0.625
    assert agg["gate_pass_rates"]["spread_ok"] == 0.625


def test_aggregate_composite_top_reasons_merged_and_summed():
    fps = [
        _fp("camino", "equity", "t1", 5, top_fail_reasons=[
            {"key": "gap_below_1_pct", "count": 3},
            {"key": "spread_too_wide", "count": 2},
        ]),
        _fp("camino", "equity", "t2", 5, top_fail_reasons=[
            {"key": "gap_below_1_pct", "count": 4},
            {"key": "quality_c", "count": 1},
        ]),
    ]
    agg = _aggregate_composite(fps, top_k=3)
    tops = {r["key"]: r["count"] for r in agg["top_fail_reasons"]}
    assert tops["gap_below_1_pct"] == 7
    assert tops["spread_too_wide"] == 2
    assert tops["quality_c"] == 1


def test_aggregate_composite_empty_list():
    agg = _aggregate_composite([])
    assert agg["windows_used"] == 0
    assert agg["intent_count"] == 0
    assert agg["execution_ready_rate"] == 0.0
    assert agg["quality_dist"] == {}
    assert agg["confidence_percentiles"]["p50"] is None


def test_diff_percentiles_simple_delta():
    before = {"p10": 0.5, "p50": 0.7, "p90": 0.9}
    after = {"p10": 0.6, "p50": 0.75, "p90": 0.95}
    d = _diff_percentiles(before, after)
    assert d == {"p10": 0.1, "p50": 0.05, "p90": 0.05}


def test_diff_percentiles_none_side_produces_none_delta():
    before = {"p10": 0.5, "p50": None, "p90": 0.9}
    after = {"p10": 0.6, "p50": 0.75, "p90": 0.95}
    d = _diff_percentiles(before, after)
    assert d["p50"] is None


def test_diff_pct_dict_covers_union_of_keys():
    b = {"A_QUALITY": 0.5, "C_QUALITY": 0.5}
    a = {"A_QUALITY": 0.3, "B_QUALITY": 0.2, "C_QUALITY": 0.5}
    d = _diff_pct_dict(b, a)
    assert d["A_QUALITY"] == -0.2
    assert d["B_QUALITY"] == 0.2  # new bucket
    assert d["C_QUALITY"] == 0.0


def test_diff_top_reasons_new_and_dropped_and_deltas():
    before = [
        {"key": "gap_below_1_pct", "count": 6},
        {"key": "spread_too_wide", "count": 2},
    ]
    after = [
        {"key": "gap_below_1_pct", "count": 2},
        {"key": "quality_c", "count": 3},
    ]
    d = _diff_top_reasons(before, after)
    assert d["new_in_after"] == ["quality_c"]
    assert d["dropped_from_before"] == ["spread_too_wide"]
    assert d["count_deltas"]["gap_below_1_pct"] == -4  # 2 - 6
    assert d["count_deltas"]["quality_c"] == 3
    assert d["count_deltas"]["spread_too_wide"] == -2


# ─────────────────────── diff_fingerprints DB round-trip ───────────────────────


@pytest.mark.asyncio
async def test_diff_fingerprints_end_to_end_shifts_are_captured():
    """Seed a BEFORE (all C_QUALITY, all blocked) and an AFTER (mix of
    A/B, some execution-ready) and verify the diff surfaces the shift."""
    brain = "camino"
    lane = "equity"

    before_start = datetime(2020, 7, 8, 10, 0, tzinfo=timezone.utc)
    after_start = datetime(2020, 7, 8, 13, 0, tzinfo=timezone.utc)

    # BEFORE — 2 windows, 10 intents each, all C_QUALITY + no exec-ready
    for i in range(2):
        end_ts = (before_start + timedelta(minutes=15 * (i + 1))).isoformat()
        await db[SESSION_FINGERPRINTS].insert_one(_fp(
            brain, lane, end_ts, intent_count=10,
            execution_ready_rate=0.0,
            quality_dist={"C_QUALITY": 10},
            gate_pass_rates={"has_volume": 0.0, "spread_ok": 1.0},
            top_fail_reasons=[
                {"key": "relative_volume_below_threshold", "count": 10},
                {"key": "gap_below_1_pct", "count": 10},
            ],
            confidence_percentiles={"p10": 0.4, "p50": 0.5, "p90": 0.6},
            risk_multiplier_p50=0.25,
            market_regime_dist={"choppy": 10},
        ))

    # AFTER — 2 windows, 10 intents each, mostly B_QUALITY + 40% ready
    for i in range(2):
        end_ts = (after_start + timedelta(minutes=15 * (i + 1))).isoformat()
        await db[SESSION_FINGERPRINTS].insert_one(_fp(
            brain, lane, end_ts, intent_count=10,
            execution_ready_rate=0.4,
            quality_dist={"A_QUALITY": 3, "B_QUALITY": 5, "C_QUALITY": 2},
            gate_pass_rates={"has_volume": 0.8, "spread_ok": 1.0},
            top_fail_reasons=[
                {"key": "gap_below_1_pct", "count": 6},
            ],
            confidence_percentiles={"p10": 0.6, "p50": 0.7, "p90": 0.85},
            risk_multiplier_p50=0.60,
            market_regime_dist={"bull": 6, "choppy": 4},
        ))

    result = await diff_fingerprints(
        brain=brain, lane=lane,
        before_start_ts=before_start.isoformat(),
        before_end_ts=(before_start + timedelta(hours=1)).isoformat(),
        after_start_ts=after_start.isoformat(),
        after_end_ts=(after_start + timedelta(hours=1)).isoformat(),
    )

    assert result["brain"] == brain
    assert result["lane"] == lane
    assert result["before"]["windows_used"] == 2
    assert result["after"]["windows_used"] == 2
    assert result["before"]["intent_count"] == 20
    assert result["after"]["intent_count"] == 20

    d = result["deltas"]
    assert d["intent_count"] == 0
    # exec-ready rate rose from 0 to 0.4
    assert d["execution_ready_rate"] == 0.4
    # gate_pass_rates.has_volume rose from 0 to 0.8
    assert d["gate_pass_rates"]["has_volume"] == 0.8
    # quality shifted OUT of C, INTO A and B.
    # BEFORE: C 20/20 = 1.0.
    # AFTER:  A 6/20 = 0.3, B 10/20 = 0.5, C 4/20 = 0.2.
    assert d["quality_dist_pct"]["A_QUALITY"] == 0.3
    assert d["quality_dist_pct"]["B_QUALITY"] == 0.5
    assert d["quality_dist_pct"]["C_QUALITY"] == -0.8
    # confidence p50 rose
    assert d["confidence_percentiles"]["p50"] == 0.2   # 0.7 - 0.5
    # risk_multiplier_p50 rose
    assert d["risk_multiplier_p50"] == 0.35
    # top_fail_reasons: relative_volume_below_threshold DROPPED from
    # the after top list; gap_below_1_pct count fell from 20 to 12.
    tr = d["top_fail_reasons"]
    assert "relative_volume_below_threshold" in tr["dropped_from_before"]
    assert tr["count_deltas"]["gap_below_1_pct"] == -8


@pytest.mark.asyncio
async def test_diff_fingerprints_empty_before_and_after():
    """No fingerprints in either range → both aggregates report zero,
    no exception."""
    result = await diff_fingerprints(
        brain="camino", lane="equity",
        before_start_ts="2020-01-01T00:00:00+00:00",
        before_end_ts="2020-01-01T01:00:00+00:00",
        after_start_ts="2020-01-02T00:00:00+00:00",
        after_end_ts="2020-01-02T01:00:00+00:00",
    )
    assert result["before"]["intent_count"] == 0
    assert result["after"]["intent_count"] == 0
    assert result["deltas"]["intent_count"] == 0
    assert result["deltas"]["execution_ready_rate"] == 0.0
    # No new reasons in either side.
    assert result["deltas"]["top_fail_reasons"]["new_in_after"] == []


@pytest.mark.asyncio
async def test_diff_fingerprints_invalid_brain_raises():
    with pytest.raises(ValueError, match="invalid brain"):
        await diff_fingerprints(
            brain="not_a_brain", lane="equity",
            before_start_ts="2026-01-01T00:00:00+00:00",
            before_end_ts="2026-01-01T01:00:00+00:00",
            after_start_ts="2026-01-01T02:00:00+00:00",
            after_end_ts="2026-01-01T03:00:00+00:00",
        )


@pytest.mark.asyncio
async def test_diff_fingerprints_invalid_lane_raises():
    with pytest.raises(ValueError, match="invalid lane"):
        await diff_fingerprints(
            brain="camino", lane="options",
            before_start_ts="2026-01-01T00:00:00+00:00",
            before_end_ts="2026-01-01T01:00:00+00:00",
            after_start_ts="2026-01-01T02:00:00+00:00",
            after_end_ts="2026-01-01T03:00:00+00:00",
        )


@pytest.mark.asyncio
async def test_diff_fingerprints_only_before_populated_surfaces_starve():
    """Fingerprints in BEFORE window only, AFTER window empty →
    surfaces `execution_ready_rate` delta as negative + intent_count
    delta as negative. This is exactly the "did I starve a lane"
    signal the tool exists for."""
    end_ts = datetime(2020, 7, 8, 10, 15, tzinfo=timezone.utc).isoformat()
    await db[SESSION_FINGERPRINTS].insert_one(_fp(
        "camino", "equity", end_ts, intent_count=10,
        execution_ready_rate=0.5,
        quality_dist={"A_QUALITY": 5, "B_QUALITY": 5},
    ))
    result = await diff_fingerprints(
        brain="camino", lane="equity",
        before_start_ts="2020-07-08T10:00:00+00:00",
        before_end_ts="2020-07-08T11:00:00+00:00",
        after_start_ts="2020-07-08T13:00:00+00:00",
        after_end_ts="2020-07-08T14:00:00+00:00",
    )
    assert result["before"]["intent_count"] == 10
    assert result["after"]["intent_count"] == 0
    assert result["deltas"]["intent_count"] == -10
    assert result["deltas"]["execution_ready_rate"] == -0.5
    # C_QUALITY / A_QUALITY dropped by their pre-existing share
    assert result["deltas"]["quality_dist_pct"]["A_QUALITY"] == -0.5
