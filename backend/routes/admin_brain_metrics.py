"""Brain metrics admin route — five operator-tracked KPIs.

Endpoints:
  GET /api/admin/brain-metrics            — current window snapshot
  GET /api/admin/brain-metrics/history    — snapshot timeseries

Doctrine:
  * READ-ONLY w.r.t. the execution path. Does NOT touch shared_intents,
    pipeline_receipts, or any pipeline collection.
  * Each call to the current-window endpoint ALSO writes a row to
    `brain_metrics_snapshots` so the history endpoint can timeseries-
    render the trend. This is the same call-driven pattern the funnel
    uses (no separate scheduler needed; the UI's 60s poll IS the
    scheduler).
  * 72h snapshot retention (best-effort prune on every call).

Operator request (2026-02):
> "We need to track these over the next few days:
>  HOLD count, Entropy average, Reason-code distribution,
>  Lane-specific decisions, Probability spread."
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import SHARED_INTENTS
from shared.pipeline.receipts import PIPELINE_RECEIPTS_COLL
from shared.brain_metrics import (
    consensus_boost_applied_rate,
    count_holds,
    entropy_average,
    lane_specific_decisions,
    probability_spread,
    reason_code_distribution,
)


router = APIRouter(prefix="/admin/brain-metrics", tags=["admin-brain-metrics"])

BRAIN_METRICS_SNAPSHOTS_COLL = "brain_metrics_snapshots"
SNAPSHOT_RETENTION_HOURS = 72


async def _compute_metrics_for_window(hours: int) -> Dict[str, Any]:
    """Run all five computations against the last N hours of intents."""
    cutoff_iso = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()

    # Pull intents + the fields the five computations need.
    intents: List[Dict[str, Any]] = await db[SHARED_INTENTS].find(
        {"ingest_ts": {"$gte": cutoff_iso}},
        {
            "_id": 0, "intent_id": 1, "stack": 1, "brain_id": 1, "lane": 1,
            "action": 1, "symbol": 1, "ingest_ts": 1, "confidence": 1,
            "gate_state": 1, "plan": 1,
        },
    ).to_list(length=50000)

    intent_ids = [i.get("intent_id") for i in intents if i.get("intent_id")]
    receipts_by_id: Dict[str, Dict[str, Any]] = {}
    if intent_ids:
        receipts = await db[PIPELINE_RECEIPTS_COLL].find(
            {"intent_id": {"$in": intent_ids}},
            {"_id": 0, "intent_id": 1, "final_reason": 1, "final_status": 1},
        ).to_list(length=50000)
        receipts_by_id = {r["intent_id"]: r for r in receipts}

    return {
        "window_hours": hours,
        "total_intents": len(intents),
        "holds": count_holds(intents),
        "entropy": entropy_average(intents),
        "reason_codes": reason_code_distribution(intents, receipts_by_id),
        "lane_decisions": lane_specific_decisions(intents),
        "probability_spread": probability_spread(intents),
        "consensus_boost": await consensus_boost_applied_rate(db, hours),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _snapshot_doc(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten the metric payload into a snapshot row optimized for
    timeseries reads.
    """
    return {
        "window_hours": payload["window_hours"],
        "total_intents": payload["total_intents"],
        # Holds — primary numbers only (no per-brain detail — keeps the
        # snapshot row small; per-brain detail is on the live endpoint).
        "hold_combined": payload["holds"]["combined"],
        "hold_v2": payload["holds"]["v2_hold"],
        "hold_v3_total": payload["holds"]["v3_total"],
        # Entropy
        "entropy_mean": payload["entropy"]["mean_across_brains"],
        "entropy_median": payload["entropy"]["median_across_brains"],
        # Reason codes — top 3 only on the snapshot (the live endpoint
        # surfaces the full top 15).
        "top_gate_states": payload["reason_codes"]["top_gate_states"][:3],
        # Lane totals — just the per-lane intent count + most-common
        # decision (full distribution on live endpoint).
        "lane_totals": {
            ln: {"total": d.get("total", 0)}
            for ln, d in payload["lane_decisions"].items()
        },
        # Probability spread
        "prob_spread_mean": payload["probability_spread"]["mean_spread"],
        "prob_spread_median": payload["probability_spread"]["median_spread"],
        "prob_spread_max": payload["probability_spread"]["max_spread"],
        "prob_spread_buckets": payload["probability_spread"]["n_disagreement_buckets"],
        # Consensus boost applied rate (operator KPI 2026-06-24).
        "consensus_applied_rate": payload["consensus_boost"]["applied_rate"],
        "consensus_applied_count": payload["consensus_boost"]["applied_count"],
        "consensus_total_evaluated": payload["consensus_boost"]["total_evaluated"],
        "consensus_health_band": payload["consensus_boost"]["health_band"],
        "captured_at": datetime.now(timezone.utc),
    }


async def _record_snapshot(payload: Dict[str, Any]) -> None:
    """Write snapshot row + best-effort retention prune. Never raises."""
    try:
        await db[BRAIN_METRICS_SNAPSHOTS_COLL].insert_one(_snapshot_doc(payload))
    except Exception:
        # Snapshot persistence is housekeeping — never fail the live
        # read for a write hiccup.
        return
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=SNAPSHOT_RETENTION_HOURS
        )
        await db[BRAIN_METRICS_SNAPSHOTS_COLL].delete_many(
            {"captured_at": {"$lt": cutoff}}
        )
    except Exception:
        pass


@router.get("")
async def brain_metrics_current(
    hours: int = Query(default=24, ge=1, le=168),
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """All five metrics computed over the last `hours` hours.

    Side-effect: writes a snapshot row to `brain_metrics_snapshots`
    so the history endpoint can render trends. Same pattern as the
    funnel endpoint.
    """
    payload = await _compute_metrics_for_window(hours)
    await _record_snapshot(payload)
    return payload


@router.get("/history")
async def brain_metrics_history(
    hours: int = Query(default=72, ge=1, le=168),
    window_hours: int = Query(default=24, ge=1, le=168),
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Timeseries of snapshot rows for the given `window_hours` size,
    going back `hours` hours.

    Operator can sparkline each metric across the full multi-day
    observation window.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = await db[BRAIN_METRICS_SNAPSHOTS_COLL].find(
        {"window_hours": window_hours, "captured_at": {"$gte": cutoff}},
        {"_id": 0},
    ).sort("captured_at", 1).to_list(length=5000)

    # Serialize datetime → iso string for JSON.
    for r in rows:
        if isinstance(r.get("captured_at"), datetime):
            r["captured_at"] = r["captured_at"].isoformat()

    return {
        "hours": hours,
        "window_hours": window_hours,
        "n_snapshots": len(rows),
        "snapshots": rows,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
