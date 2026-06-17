"""/api/intents/{intent_id}/why — single-document answer to
"who stopped this trade?".

Reads `pipeline_receipts` for the unified pipeline's verdict, then
augments with the raw `shared_intents` row so the operator sees
exactly what the brain emitted vs. what the pipeline did.

Returns a flat dict with the four canonical fields plus context:
    final_status        : NO_ORDER | BLOCKED | DECISION_LOGGED | SUBMITTED | BROKER_ERROR
    final_reason        : canonical string
    restriction_source  : brain | seat | roadguard | broker
    broker_called       : bool

If no pipeline receipt exists (intent was processed by the legacy chain),
returns final_status="NO_RECEIPT" and final_reason="missing_receipt" so
operators can spot intents that escaped the new pipeline.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from db import db
from namespaces import SHARED_INTENTS
from shared.pipeline.receipts import ReceiptStore


router = APIRouter(prefix="/intents", tags=["intents-why"])


@router.get("/{intent_id}/why")
async def why_no_trade(
    intent_id: str,
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    store = ReceiptStore()
    receipt = await store.find_by_intent(intent_id)
    intent = await db[SHARED_INTENTS].find_one(
        {"intent_id": intent_id}, {"_id": 0},
    )
    if not intent and not receipt:
        raise HTTPException(status_code=404, detail=f"intent_not_found:{intent_id}")

    intent = intent or {}
    if not receipt:
        return {
            "intent_id": intent_id,
            "brain": intent.get("stack") or intent.get("brain_id"),
            "symbol": intent.get("symbol"),
            "action": intent.get("action"),
            "confidence": intent.get("confidence"),
            "final_status": "NO_RECEIPT",
            "final_reason": "intent_processed_by_legacy_chain_or_not_yet_evaluated",
            "restriction_source": "unknown",
            "broker_called": False,
            "requested_notional": intent.get("requested_notional_usd"),
            "final_notional": None,
        }

    return {
        "intent_id": intent_id,
        "brain": receipt.get("brain_id") or intent.get("stack"),
        "symbol": receipt.get("symbol") or intent.get("symbol"),
        "action": receipt.get("action") or intent.get("action"),
        "confidence": receipt.get("confidence", intent.get("confidence")),
        "lane": receipt.get("lane") or intent.get("lane"),
        "final_status": receipt.get("final_status", "NO_RECEIPT"),
        "final_reason": receipt.get("final_reason", "missing_receipt"),
        "restriction_source": receipt.get("restriction_source", "unknown"),
        "broker_called": bool(receipt.get("broker_called", False)),
        "requested_notional": receipt.get("requested_notional"),
        "final_notional": receipt.get("final_notional"),
        "autonomy_mode": receipt.get("autonomy_mode"),
        "governor_multiplier": receipt.get("governor_multiplier"),
        "evidence_snapshot": receipt.get("evidence_snapshot") or {},
        "ts": receipt.get("ts"),
    }


@router.get("/_pipeline/summary")
async def pipeline_summary(
    hours: int = 24,
    _user: dict = Depends(get_current_user),  # noqa: B008
) -> Dict[str, Any]:
    """Quick aggregate for the operator UI: how many intents in the
    last N hours landed in each `restriction_source` bucket. Lets us
    confirm the unified pipeline is actually being exercised (and what
    the dominant block source is).
    """
    from datetime import datetime, timedelta, timezone
    hours = max(1, min(int(hours or 24), 168))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    pipeline = [
        {"$match": {"ts": {"$gte": cutoff}}},
        {"$group": {
            "_id": {
                "source": "$restriction_source",
                "status": "$final_status",
            },
            "count": {"$sum": 1},
        }},
    ]
    rows = await db["pipeline_receipts"].aggregate(pipeline).to_list(length=200)
    by_source: Dict[str, int] = {}
    by_status: Dict[str, int] = {}
    total = 0
    for r in rows:
        c = int(r.get("count", 0))
        total += c
        s = (r["_id"] or {}).get("source") or "unknown"
        st = (r["_id"] or {}).get("status") or "unknown"
        by_source[s] = by_source.get(s, 0) + c
        by_status[st] = by_status.get(st, 0) + c
    return {
        "window_hours": hours,
        "total_receipts": total,
        "by_restriction_source": by_source,
        "by_final_status": by_status,
    }
