"""Pipeline blocker histogram — operator-facing diagnostic.

Read-only summary of WHY recent intents have been blocked or
executed, grouped by lane × restriction_source × final_reason.
Built 2026-06-19 to answer "why isn't equity trading?" without
having the operator scroll through hundreds of expanded intent
rows on their phone.

Returns the last `hours` (default 24) of `pipeline_receipts` rows,
bucketed for at-a-glance triage. Empty buckets are omitted from
the response so the JSON stays small.

Authentication: admin JWT.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db


router = APIRouter(prefix="/admin/pipeline", tags=["pipeline-diagnostics"])


@router.get("/recent-blocker-histogram")
async def recent_blocker_histogram(
    hours: int = Query(default=24, ge=1, le=168),
    lane: Optional[str] = Query(default=None),
    _user: dict = Depends(get_current_user),
) -> dict:
    """Group recent pipeline receipts by lane × restriction_source ×
    final_reason. Operator's first-line diagnostic when intents
    aren't flowing.

    Response shape:
        {
          "window_hours": 24,
          "now": "<ISO ts>",
          "total_receipts": <int>,
          "by_lane": {
            "equity": {
              "total": <int>,
              "executed": <int>,
              "blocked": <int>,
              "blockers": [
                {"source": "roadguard", "reason": "market_closed", "count": 42},
                {"source": "seat", "reason": "below_seat_confidence_min:0.412<0.700", "count": 18},
                ...
              ]
            },
            "crypto": { ... }
          }
        }
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    q: dict = {"ts": {"$gte": cutoff}}
    if lane:
        q["lane"] = lane

    rows = await db["pipeline_receipts"].find(
        q,
        {
            "_id": 0,
            "lane": 1,
            "verdict": 1,
            "final_status": 1,
            "restriction_source": 1,
            "final_reason": 1,
            "reason": 1,
            "ts": 1,
            "symbol": 1,
            "brain_id": 1,
        },
    ).sort("ts", -1).to_list(50000)

    by_lane: dict[str, dict] = {}
    for r in rows:
        ln = (r.get("lane") or "?").lower()
        bucket = by_lane.setdefault(ln, {
            "total": 0,
            "executed": 0,
            "blocked": 0,
            "no_order": 0,
            "broker_error": 0,
            "_blocker_counter": Counter(),
            "_recent_samples": [],
        })
        bucket["total"] += 1

        # `final_status` is the canonical state in the new pipeline;
        # fall back to `verdict` for older rows that pre-date the
        # status field.
        status = (r.get("final_status") or r.get("verdict") or "?").upper()
        if status == "SUBMITTED" or status == "EXECUTED":
            bucket["executed"] += 1
            continue
        if status == "NO_ORDER":
            bucket["no_order"] += 1
            continue
        if status == "BROKER_ERROR":
            bucket["broker_error"] += 1
        else:
            bucket["blocked"] += 1

        # Bucket the reason. Some reasons embed dynamic data like
        # `below_seat_confidence_min:0.412<0.700` — keep them whole
        # so the operator can see actual values; collapsing would
        # lose the operationally-useful number.
        src = (r.get("restriction_source") or "?").lower()
        reason = r.get("final_reason") or r.get("reason") or "(no reason)"
        bucket["_blocker_counter"][(src, reason)] += 1

        # Hold onto a sliver of recent samples for spot-checking.
        if len(bucket["_recent_samples"]) < 3:
            bucket["_recent_samples"].append({
                "ts": r.get("ts"),
                "symbol": r.get("symbol"),
                "brain_id": r.get("brain_id"),
                "status": status,
                "source": src,
                "reason": reason,
            })

    # Reshape for JSON: drop helper underscore fields, sort blockers
    # by count desc.
    for ln, b in by_lane.items():
        blockers = [
            {"source": src, "reason": reason, "count": n}
            for (src, reason), n in b["_blocker_counter"].most_common()
        ]
        b["blockers"] = blockers
        b["recent_samples"] = b.pop("_recent_samples")
        b.pop("_blocker_counter", None)

    return {
        "window_hours": hours,
        "now": datetime.now(timezone.utc).isoformat(),
        "total_receipts": len(rows),
        "by_lane": by_lane,
    }
