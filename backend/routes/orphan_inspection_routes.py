"""
Orphan inspection routes — operator visibility into broker fills that
MC did not issue.

Mount path (lives under `api_router` which prefixes `/api`):

    GET  /api/admin/runtime/orphans/recent
    GET  /api/admin/runtime/orphans/summary

These endpoints are READ-ONLY. Quarantine writes happen only via the
watchdog (or the one-shot ingester script).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db


router = APIRouter(prefix="/admin/runtime/orphans", tags=["orphan-watchdog"])


def _strip_id(d: Dict[str, Any]) -> Dict[str, Any]:
    d.pop("_id", None)
    return d


@router.get("/recent")
async def recent_orphans(
    hours: int = Query(default=24, ge=1, le=720),
    limit: int = Query(default=100, ge=1, le=500),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """List the most recent orphan quarantine entries.

    An orphan is any quarantine row tied to a UV-classified execution
    memory (either submitted UV at insert time, or blocked at the
    KernelGate routing step). Both surfaces are operator-relevant.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    query = {
        "$and": [
            {"$or": [
                {"alert_level": "CRITICAL"},
                {"reason": "classified_uv"},
            ]},
            {"created_at": {"$gte": since}},
        ],
    }
    cursor = (
        db.memory_kernel_quarantine
        .find(query)
        .sort("created_at", -1)
        .limit(limit)
    )
    rows: List[Dict[str, Any]] = []
    async for d in cursor:
        rows.append(_strip_id(d))

    enriched = []
    for q in rows:
        mid = q.get("memory_id")
        if not mid:
            enriched.append(q)
            continue
        mem = await db.memory_kernel_ledger.find_one(
            {"memory_id": mid},
            {"_id": 0, "payload": 1, "source_stack": 1, "memory_type": 1, "provenance": 1, "created_at": 1},
        )
        if mem and mem.get("memory_type") == "execution":
            # Only surface EXECUTION-class UV memories as orphans —
            # diagnostic/governance UVs aren't broker fills.
            q["memory"] = mem
            enriched.append(q)

    return {
        "ok": True,
        "since": since.isoformat(),
        "count": len(enriched),
        "items": enriched,
    }


@router.get("/summary")
async def orphan_summary(
    hours: int = Query(default=24, ge=1, le=720),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Aggregate orphan counts grouped by source / symbol / hour.

    Only EXECUTION-class UV memories are counted (i.e. actual broker
    fills MC did not issue), not diagnostic/governance UVs.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    match = {
        "$and": [
            {"$or": [
                {"alert_level": "CRITICAL"},
                {"reason": "classified_uv"},
            ]},
            {"created_at": {"$gte": since}},
        ],
    }

    # Aggregate via lookup → filter to execution-class only.
    pipeline = [
        {"$match": match},
        {"$lookup": {
            "from": "memory_kernel_ledger",
            "localField": "memory_id",
            "foreignField": "memory_id",
            "as": "memory",
        }},
        {"$unwind": {"path": "$memory", "preserveNullAndEmptyArrays": False}},
        {"$match": {"memory.memory_type": "execution"}},
    ]

    total = 0
    by_source_counter: Dict[str, int] = {}
    by_symbol_counter: Dict[str, int] = {}
    async for r in db.memory_kernel_quarantine.aggregate(pipeline):
        total += 1
        src = r["memory"].get("source_stack") or "unknown"
        sym = r["memory"].get("payload", {}).get("symbol") or "unknown"
        by_source_counter[src] = by_source_counter.get(src, 0) + 1
        by_symbol_counter[sym] = by_symbol_counter.get(sym, 0) + 1

    by_source = sorted(
        ({"source_stack": k, "count": v} for k, v in by_source_counter.items()),
        key=lambda x: -x["count"],
    )
    by_symbol = sorted(
        ({"symbol": k, "count": v} for k, v in by_symbol_counter.items()),
        key=lambda x: -x["count"],
    )[:25]

    return {
        "ok": True,
        "since": since.isoformat(),
        "total": total,
        "by_source": by_source,
        "by_symbol": by_symbol,
    }
