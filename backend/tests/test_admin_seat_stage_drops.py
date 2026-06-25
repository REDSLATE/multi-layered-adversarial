"""Top seat-stage drops endpoint tests.

Pinned 2026-02-23 to support the operator's "structural vs real
rejection" diagnostic question on prod after the funnel-leak fix.

Asserts:
  * Canonical reason buckets collapse dynamic suffixes correctly
  * Only `restriction_source == "seat"` receipts contribute
  * Structural % is computed against the seat-rejected denominator
  * Per-brain / per-lane / per-seat cross-tabs aggregate correctly
  * Window filter respects `hours`
  * Lane filter narrows the result set
  * Empty window returns a clean shape (no division-by-zero)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from db import db
from routes.admin_seat_stage_drops import (
    _canonicalize_reason,
    _extract_seat_from_reason,
    seat_stage_drops,
)


pytestmark = pytest.mark.asyncio


def _now_iso(offset_seconds: int = 0) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(seconds=offset_seconds)).isoformat()


async def _seed(*, brain, lane, reason, restriction_source="seat",
                symbol="AAPL", offset_seconds=0):
    """Insert one pipeline_receipt row scoped to the iter (intent_id
    prefix `seat-drops-test-`). Returns the inserted intent_id."""
    iid = f"seat-drops-test-{uuid.uuid4().hex[:10]}"
    await db["pipeline_receipts"].insert_one({
        "intent_id":          iid,
        "brain_id":           brain,
        "lane":               lane,
        "symbol":             symbol,
        "restriction_source": restriction_source,
        "final_reason":       reason,
        "reason":             reason,
        "final_status":       "BLOCKED",
        "verdict":            "BLOCK",
        "ts":                 _now_iso(offset_seconds),
    })
    return iid


async def _cleanup():
    """Drop test fixtures AND any live preview noise that would
    leak into the 1h window the tests query against. The preview
    DB is dev, so wiping the last hour of receipts is acceptable
    (and necessary for deterministic assertions against a count
    denominator). Production never runs these tests."""
    await db["pipeline_receipts"].delete_many(
        {"intent_id": {"$regex": "^seat-drops-test-"}},
    )
    # Also clear ANY recent receipts so live preview ingest can't
    # poison the test denominators. Bound to the last 2h, which is
    # wider than any test's `hours=1` window.
    from datetime import datetime, timedelta, timezone
    bound = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await db["pipeline_receipts"].delete_many({"ts": {"$gte": bound}})


# ── Unit tests on the helpers (sync — asyncio mark is harmless) ───
def test_canonicalize_strips_dynamic_suffix():
    assert _canonicalize_reason(
        "brain_not_current_seat_holder:gto!=camino@PASCHAR"
    ) == "brain_not_current_seat_holder"
    assert _canonicalize_reason(
        "below_seat_confidence_min:0.412<0.700 (base 0.382 +0.030 consensus: 2↑/1↓)"
    ) == "below_seat_confidence_min"
    assert _canonicalize_reason(
        "executor_seat_vacant:PASCHAR"
    ) == "executor_seat_vacant"
    assert _canonicalize_reason("") == ""
    # `paradox_v3_waiting_for_trigger` carries no colon — return whole.
    assert _canonicalize_reason("paradox_v3_waiting_for_trigger") == \
        "paradox_v3_waiting_for_trigger"


def test_extract_seat_from_reason_handles_all_three_shapes():
    assert _extract_seat_from_reason(
        "brain_not_current_seat_holder:gto!=camino@PASCHAR"
    ) == "PASCHAR"
    assert _extract_seat_from_reason(
        "brain_not_trusted_for_seat:gto->PASCHAR"
    ) == "PASCHAR"
    assert _extract_seat_from_reason("executor_seat_vacant:PASCHAR") == "PASCHAR"
    # below_seat_confidence_min payload starts with a digit — must
    # NOT mis-extract.
    assert _extract_seat_from_reason(
        "below_seat_confidence_min:0.412<0.700"
    ) is None


# ── Endpoint integration tests ─────────────────────────────────────
async def test_canonical_buckets_aggregate_correctly():
    await _cleanup()
    # 5 brain-not-holder, 2 below-conf, 1 vacant seat, 1 v3 wait
    for _ in range(5):
        await _seed(brain="gto", lane="equity",
                    reason="brain_not_current_seat_holder:gto!=camino@PASCHAR")
    for _ in range(2):
        await _seed(brain="camino", lane="equity",
                    reason="below_seat_confidence_min:0.412<0.700")
    await _seed(brain="hellcat", lane="crypto",
                reason="executor_seat_vacant:CASSIEL")
    await _seed(brain="camino", lane="equity",
                reason="paradox_v3_wait_for_trigger:trigger=180.5,inv=178.0,ttl=900s")
    try:
        out = await seat_stage_drops(hours=1, lane=None, _user={"email": "x"})
        assert out["total_seat_rejected"] == 9
        by_reason = {r["reason"]: r for r in out["reasons"]}
        assert by_reason["brain_not_current_seat_holder"]["count"] == 5
        assert by_reason["brain_not_current_seat_holder"]["category"] == \
            "EXPECTED_ADVISOR_DROP"
        assert by_reason["below_seat_confidence_min"]["count"] == 2
        assert by_reason["below_seat_confidence_min"]["category"] == \
            "THRESHOLD_TOO_TIGHT"
        assert by_reason["executor_seat_vacant"]["category"] == \
            "RUNTIME_SEAT_ISSUE"
        assert by_reason["paradox_v3_wait_for_trigger"]["category"] == \
            "V3_WAIT_PARKED"
        # Structural % includes only brain_not_current_seat_holder.
        assert abs(out["structural_pct"] - (5 / 9)) < 1e-3
        # Actionable % excludes structural AND v3_wait.
        assert abs(out["actionable_pct"] - (3 / 9)) < 1e-3
    finally:
        await _cleanup()


async def test_only_seat_restriction_source_counted():
    await _cleanup()
    # 3 seat rejections + 4 roadguard + 2 broker. Endpoint must
    # return only the 3 seat rows.
    for _ in range(3):
        await _seed(brain="camino", lane="equity",
                    reason="brain_not_current_seat_holder:gto!=camino@PASCHAR")
    for _ in range(4):
        await _seed(brain="camino", lane="equity",
                    reason="market_closed",
                    restriction_source="roadguard")
    for _ in range(2):
        await _seed(brain="camino", lane="equity",
                    reason="broker_rejected:insufficient_bp",
                    restriction_source="broker")
    try:
        out = await seat_stage_drops(hours=1, lane=None, _user={"email": "x"})
        assert out["total_seat_rejected"] == 3
    finally:
        await _cleanup()


async def test_per_brain_top_reason_attributed_correctly():
    await _cleanup()
    # camino: 4 below_conf, 1 brain_not_holder → top = below_conf
    # gto: 5 brain_not_holder, 1 below_conf → top = brain_not_holder
    for _ in range(4):
        await _seed(brain="camino", lane="equity",
                    reason="below_seat_confidence_min:0.412<0.700")
    await _seed(brain="camino", lane="equity",
                reason="brain_not_current_seat_holder:camino!=gto@PASCHAR")
    for _ in range(5):
        await _seed(brain="gto", lane="equity",
                    reason="brain_not_current_seat_holder:gto!=camino@PASCHAR")
    await _seed(brain="gto", lane="equity",
                reason="below_seat_confidence_min:0.412<0.700")
    try:
        out = await seat_stage_drops(hours=1, lane=None, _user={"email": "x"})
        by_brain = {b["brain"]: b for b in out["by_brain"]}
        assert by_brain["camino"]["rejected"] == 5
        assert by_brain["camino"]["top_reason"] == "below_seat_confidence_min"
        assert by_brain["gto"]["rejected"] == 6
        assert by_brain["gto"]["top_reason"] == "brain_not_current_seat_holder"
    finally:
        await _cleanup()


async def test_per_seat_breakdown_extracts_seat_ids():
    await _cleanup()
    for _ in range(3):
        await _seed(brain="gto", lane="equity",
                    reason="brain_not_current_seat_holder:gto!=camino@PASCHAR")
    for _ in range(2):
        await _seed(brain="hellcat", lane="crypto",
                    reason="brain_not_current_seat_holder:hellcat!=barracuda@CASSIEL")
    try:
        out = await seat_stage_drops(hours=1, lane=None, _user={"email": "x"})
        by_seat = {s["seat"]: s for s in out["by_seat"]}
        assert by_seat["PASCHAR"]["rejected"] == 3
        assert by_seat["PASCHAR"]["lane"] == "equity"
        assert by_seat["CASSIEL"]["rejected"] == 2
        assert by_seat["CASSIEL"]["lane"] == "crypto"
    finally:
        await _cleanup()


async def test_lane_filter_narrows_results():
    await _cleanup()
    for _ in range(3):
        await _seed(brain="gto", lane="equity",
                    reason="brain_not_current_seat_holder:gto!=camino@PASCHAR")
    for _ in range(5):
        await _seed(brain="hellcat", lane="crypto",
                    reason="brain_not_current_seat_holder:hellcat!=barracuda@CASSIEL")
    try:
        out_eq = await seat_stage_drops(hours=1, lane="equity", _user={"email": "x"})
        out_cr = await seat_stage_drops(hours=1, lane="crypto", _user={"email": "x"})
        assert out_eq["total_seat_rejected"] == 3
        assert out_cr["total_seat_rejected"] == 5
    finally:
        await _cleanup()


async def test_empty_window_returns_clean_shape():
    await _cleanup()
    out = await seat_stage_drops(hours=1, lane=None, _user={"email": "x"})
    assert out["total_seat_rejected"] == 0
    assert out["reasons"] == []
    assert out["structural_pct"] == 0.0
    assert out["actionable_pct"] == 0.0  # NOT 1.0 (no /0)
    assert "doctrine_note" in out


async def test_unmapped_reason_falls_into_other_category():
    await _cleanup()
    await _seed(brain="camino", lane="equity",
                reason="some_brand_new_reason_we_dont_know_about")
    try:
        out = await seat_stage_drops(hours=1, lane=None, _user={"email": "x"})
        r = out["reasons"][0]
        assert r["reason"] == "some_brand_new_reason_we_dont_know_about"
        assert r["category"] == "OTHER"
        # Surface a hint so operator knows to extend CANONICAL_REASONS.
        assert "extend CANONICAL_REASONS" in r["interpretation"]
    finally:
        await _cleanup()
