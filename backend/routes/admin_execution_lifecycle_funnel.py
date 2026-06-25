"""Execution Lifecycle Funnel — what happens AFTER broker acceptance.

Doctrine: Phase B/C closed the identity-drift blind spot. The next
operational blind spot is "after MC marks an intent executed
(broker accepted the order), what actually happens to it?" Five
canonical outcomes from `shared/broker_status_classifier.py`:

    filled / partially_filled / canceled / working / unknown

This endpoint joins MC-side `shared_intents{executed:True}` to the
broker-side `broker_orders` table via `broker_order_id`, classifies
each pairing, and returns:

  * bucket_counts          : { filled: N, partially_filled: N, ... }
  * percentages            : same shape, % of executed window
  * unknown_intent_ids     : sample of intent_ids in the UNKNOWN bucket
                             (operator can debug missing receipts)
  * window                 : hours requested + the ISO timestamps used

Per-lane breakdown is included so the operator can see equity
flowing fine while crypto is stuck at "working", or vice versa.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import EXECUTION_RECEIPTS, SHARED_INTENTS
from shared.broker_status_classifier import (
    ALL_BUCKETS,
    BUCKET_UNKNOWN,
    classify_broker_status,
    empty_bucket_counts,
)


router = APIRouter(tags=["admin"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("/admin/execution-lifecycle/funnel")
async def execution_lifecycle_funnel(
    hours: int = Query(default=24, ge=1, le=720),
    lane: str | None = Query(default=None),
    user: dict = Depends(get_current_user),  # noqa: B008, ARG001
):
    """Return the 5-bucket lifecycle breakdown for intents that were
    executed in the last `hours` window.

    `lane` (optional): filter to `equity` or `crypto` only — the
    funnel often diverges per lane (different brokers, different
    market hours), so the per-lane view is operationally important.
    """
    since = (_now() - timedelta(hours=hours)).isoformat()

    # 1. Pull every executed intent in the window. Project only the
    #    fields we need for the join + classification.
    intent_q: dict = {
        "executed": True,
        "executed_at": {"$gte": since},
    }
    if lane in ("equity", "crypto"):
        intent_q["lane"] = lane

    intent_proj = {
        "_id": 0, "intent_id": 1, "broker_order_id": 1,
        "stack_canonical": 1, "stack": 1,
        "symbol": 1, "lane": 1, "action": 1, "executed_at": 1,
    }
    intents = await db[SHARED_INTENTS].find(intent_q, intent_proj).to_list(None)
    total_executed = len(intents)

    # 2. Bulk-fetch broker_orders for these intent's broker_order_ids
    #    in a single round-trip — much faster than per-intent find_one
    #    when the window has hundreds of intents.
    broker_order_ids = [
        i["broker_order_id"] for i in intents
        if i.get("broker_order_id")
    ]
    bo_map: dict[str, dict] = {}
    if broker_order_ids:
        cursor = db.broker_orders.find(
            {"broker_order_id": {"$in": broker_order_ids}},
            {"_id": 0, "broker_order_id": 1, "status": 1,
             "filled_qty": 1, "ordered_qty": 1, "qty": 1,
             "filled_avg_price": 1, "filled_at": 1, "submitted_at": 1},
        )
        async for bo in cursor:
            bo_map[bo["broker_order_id"]] = bo

    # 3. Also peek at the execution_receipts table for intents that
    #    don't have a broker_orders row — the receipt's own status
    #    snapshot can answer "is this a transient unknown or did MC
    #    never get a status back at all?"
    receipt_ids_needed = [
        i["intent_id"] for i in intents
        if i.get("broker_order_id") not in bo_map
    ]
    receipt_map: dict[str, dict] = {}
    if receipt_ids_needed:
        cursor = db[EXECUTION_RECEIPTS].find(
            {"intent_id": {"$in": receipt_ids_needed}},
            {"_id": 0, "intent_id": 1, "status": 1,
             "filled_qty": 1, "filled_avg_price": 1, "filled_at": 1},
        )
        async for r in cursor:
            receipt_map[r["intent_id"]] = r

    # 4. Classify each intent into a bucket.
    bucket_counts = empty_bucket_counts()
    bucket_by_lane: dict[str, dict[str, int]] = {
        "equity": empty_bucket_counts(),
        "crypto": empty_bucket_counts(),
    }
    bucket_by_brain: dict[str, dict[str, int]] = {}
    unknown_samples: list[dict] = []

    for it in intents:
        boid = it.get("broker_order_id")
        bo = bo_map.get(boid) if boid else None
        # Authoritative source: broker_orders (poller-updated). Fall
        # back to execution_receipts which is the submit-time snapshot.
        if bo:
            status = bo.get("status")
            filled_qty = bo.get("filled_qty")
            ordered_qty = bo.get("ordered_qty") or bo.get("qty")
        else:
            r = receipt_map.get(it["intent_id"])
            if r:
                status = r.get("status")
                filled_qty = r.get("filled_qty")
                ordered_qty = None
            else:
                status = None
                filled_qty = None
                ordered_qty = None

        bucket = classify_broker_status(
            status, filled_qty=filled_qty, ordered_qty=ordered_qty,
        )
        bucket_counts[bucket] += 1
        lane_v = (it.get("lane") or "").lower()
        if lane_v in bucket_by_lane:
            bucket_by_lane[lane_v][bucket] += 1
        # 2026-02-23: canonical-only per-brain breakdown.
        brain = (
            it.get("stack_canonical") or it.get("stack") or "unknown"
        ).lower()
        if brain not in bucket_by_brain:
            bucket_by_brain[brain] = empty_bucket_counts()
        bucket_by_brain[brain][bucket] += 1

        if bucket == BUCKET_UNKNOWN and len(unknown_samples) < 10:
            unknown_samples.append({
                "intent_id": it["intent_id"],
                "symbol": it.get("symbol"),
                "lane": it.get("lane"),
                "action": it.get("action"),
                "executed_at": it.get("executed_at"),
                "has_broker_order_id": bool(boid),
                "has_broker_orders_row": bool(bo),
                "has_execution_receipt": it["intent_id"] in receipt_map,
                "broker_status": status,
            })

    # 5. Build response. Percentages are computed against the executed
    #    total so the funnel sums to 100% (unknown included).
    pct = {
        b: (round(100.0 * bucket_counts[b] / total_executed, 1)
            if total_executed else 0.0)
        for b in ALL_BUCKETS
    }

    return {
        "ok": True,
        "window_hours": hours,
        "lane_filter": lane,
        "since": since,
        "total_executed": total_executed,
        "bucket_counts": bucket_counts,
        "bucket_percentages": pct,
        "bucket_order": list(ALL_BUCKETS),
        "by_lane": bucket_by_lane,
        "by_brain": bucket_by_brain,
        "unknown_samples": unknown_samples,
        "doctrine_note": (
            "Executed = MC's submit pipeline accepted the order and "
            "wrote a broker_order_id. The five buckets answer "
            "'what then?': filled (broker confirmed full fill), "
            "partially_filled (broker reports fill < ordered_qty), "
            "working (broker accepted, no fill yet), "
            "canceled (canceled / rejected / expired), "
            "unknown (no broker_orders row found — poller may be lagging, "
            "or the order was placed via a path that bypasses the poller)."
        ),
    }
