"""Tests for the Distribution Snapshot Job (session_fingerprint).

Covers:
    - Percentile computation edge cases
    - _compute_fingerprint aggregates all metrics correctly
    - Idempotent upsert on same _id
    - Empty window produces a zero-count fingerprint (still written)
    - Cadence drift sentinel fires on stale brains
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "/app/backend")

from db import db
from namespaces import SESSION_FINGERPRINTS, SHARED_INTENTS
from shared.session_fingerprint import (
    _compute_fingerprint,
    _percentiles,
    _tick,
    run_now,
)


@pytest.fixture(autouse=True)
async def _clean_state():
    await db[SESSION_FINGERPRINTS].delete_many({"_id": {"$regex": "^fp-test-"}})
    await db[SHARED_INTENTS].delete_many({"intent_id": {"$regex": "^fp-test-"}})
    yield
    await db[SESSION_FINGERPRINTS].delete_many({"_id": {"$regex": "^fp-test-"}})
    await db[SHARED_INTENTS].delete_many({"intent_id": {"$regex": "^fp-test-"}})


# ─────────────────────── percentile helper ───────────────────────


def test_percentiles_empty_returns_none():
    result = _percentiles([], (0.1, 0.5, 0.9))
    assert result == {"p10": None, "p50": None, "p90": None}


def test_percentiles_single_value():
    result = _percentiles([5.0], (0.1, 0.5, 0.9))
    assert result == {"p10": 5.0, "p50": 5.0, "p90": 5.0}


def test_percentiles_linear_interpolation():
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = _percentiles(vals, (0.5,))
    assert result["p50"] == 3.0


def test_percentiles_unsorted_input_still_correct():
    """Impl should sort internally."""
    result = _percentiles([5.0, 1.0, 3.0, 2.0, 4.0], (0.5,))
    assert result["p50"] == 3.0


# ─────────────────────── _compute_fingerprint ───────────────────────


async def _insert_test_intent(**overrides):
    """Helper — insert a minimal intent matching the projection shape.
    Uses a distinct `stack_canonical` (`fp-test-brain`) so tests
    don't collide with the live production intent stream."""
    doc = {
        "intent_id": f"fp-test-{uuid.uuid4()}",
        "stack_canonical": overrides.get("stack_canonical", "fp-test-brain"),
        "lane": "equity",
        "ingest_ts": overrides.get(
            "ingest_ts",
            (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        ),
        "gate_state": overrides.get("gate_state", "blocked"),
        "confidence": overrides.get("confidence", 0.7),
        "risk_multiplier": overrides.get("risk_multiplier", 0.6),
        "doctrine_packet": {
            "base_labels": {
                "quality": overrides.get("quality", "C_QUALITY"),
                "labels": overrides.get("labels", ["LARGE_CAP_LIQUID"]),
                "reasons": overrides.get(
                    "reasons", ["relative_volume_below_threshold"],
                ),
            },
            "seats": {
                "adversary": {
                    "objections": overrides.get(
                        "objections", ["rvol_too_quiet_for_directional"],
                    ),
                },
                "execution_judge": {
                    "execution_ready": overrides.get("execution_ready", False),
                    "execution_checks": overrides.get(
                        "execution_checks",
                        {"quality_ok": True, "has_volume": False},
                    ),
                },
            },
        },
        "snapshot": {
            "relative_volume": overrides.get("relative_volume", 0.5),
            "gap_pct": overrides.get("gap_pct", 0.2),
            "market_regime": overrides.get("market_regime", "choppy"),
        },
    }
    await db[SHARED_INTENTS].insert_one(doc)
    return doc


@pytest.mark.asyncio
async def test_fingerprint_counts_intents_in_window():
    start = datetime.now(timezone.utc) - timedelta(minutes=15)
    end = datetime.now(timezone.utc)
    for _ in range(3):
        await _insert_test_intent(
            ingest_ts=(start + timedelta(minutes=5)).isoformat(),
        )
    # Insert one OUTSIDE the window.
    await _insert_test_intent(
        ingest_ts=(start - timedelta(minutes=5)).isoformat(),
    )

    fp = await _compute_fingerprint(
        brain="fp-test-brain", lane="equity",
        window_start_iso=start.isoformat(), window_end_iso=end.isoformat(),
        top_k=5,
    )
    assert fp["intent_count"] == 3
    # gate_state histogram
    assert fp["gate_state_dist"].get("blocked") == 3


@pytest.mark.asyncio
async def test_fingerprint_captures_quality_distribution():
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=15)
    for q in ("A_QUALITY", "A_QUALITY", "B_QUALITY", "C_QUALITY", "C_QUALITY"):
        await _insert_test_intent(
            ingest_ts=(now - timedelta(minutes=1)).isoformat(),
            quality=q,
        )
    fp = await _compute_fingerprint(
        brain="fp-test-brain", lane="equity",
        window_start_iso=window_start.isoformat(),
        window_end_iso=now.isoformat(),
        top_k=5,
    )
    assert fp["quality_dist"]["A_QUALITY"] == 2
    assert fp["quality_dist"]["B_QUALITY"] == 1
    assert fp["quality_dist"]["C_QUALITY"] == 2


@pytest.mark.asyncio
async def test_fingerprint_captures_top_fail_reasons():
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=15)
    for reasons in (
        ["a", "b"],
        ["a"],
        ["a", "c"],
        ["b", "c"],
    ):
        await _insert_test_intent(
            ingest_ts=(now - timedelta(minutes=1)).isoformat(),
            reasons=reasons,
        )
    fp = await _compute_fingerprint(
        brain="fp-test-brain", lane="equity",
        window_start_iso=window_start.isoformat(),
        window_end_iso=now.isoformat(),
        top_k=3,
    )
    # `a` is in 3 intents, `b` in 2, `c` in 2.
    top = {r["key"]: r["count"] for r in fp["top_fail_reasons"]}
    assert top["a"] == 3
    assert top["b"] == 2
    assert top["c"] == 2


@pytest.mark.asyncio
async def test_fingerprint_execution_ready_rate():
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=15)
    for ready in (True, True, False, False, False):
        await _insert_test_intent(
            ingest_ts=(now - timedelta(minutes=1)).isoformat(),
            execution_ready=ready,
        )
    fp = await _compute_fingerprint(
        brain="fp-test-brain", lane="equity",
        window_start_iso=window_start.isoformat(),
        window_end_iso=now.isoformat(),
        top_k=5,
    )
    assert fp["execution_ready_rate"] == 0.4


@pytest.mark.asyncio
async def test_fingerprint_rvol_percentiles():
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=15)
    for rv in (0.5, 1.0, 1.5, 2.0, 2.5):
        await _insert_test_intent(
            ingest_ts=(now - timedelta(minutes=1)).isoformat(),
            relative_volume=rv,
        )
    fp = await _compute_fingerprint(
        brain="fp-test-brain", lane="equity",
        window_start_iso=window_start.isoformat(),
        window_end_iso=now.isoformat(),
        top_k=5,
    )
    assert fp["rvol_percentiles"]["p50"] == 1.5


@pytest.mark.asyncio
async def test_fingerprint_market_regime_distribution():
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=15)
    for regime in ("bull", "bull", "bull", "choppy", "bear"):
        await _insert_test_intent(
            ingest_ts=(now - timedelta(minutes=1)).isoformat(),
            market_regime=regime,
        )
    fp = await _compute_fingerprint(
        brain="fp-test-brain", lane="equity",
        window_start_iso=window_start.isoformat(),
        window_end_iso=now.isoformat(),
        top_k=5,
    )
    assert fp["market_regime_dist"] == {"bull": 3, "choppy": 1, "bear": 1}


@pytest.mark.asyncio
async def test_fingerprint_empty_window_returns_zero_counts():
    """Empty window still produces a valid doc — quiet periods matter
    for the audit trail (a 15-min gap tells you as much as a busy
    window)."""
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=15)
    fp = await _compute_fingerprint(
        brain="fp-test-brain", lane="equity",
        window_start_iso=window_start.isoformat(),
        window_end_iso=now.isoformat(),
        top_k=5,
    )
    assert fp["intent_count"] == 0
    assert fp["execution_ready_rate"] == 0.0
    assert fp["rvol_percentiles"] == {"p10": None, "p50": None, "p90": None}
