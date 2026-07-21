"""Gate Failure Digest — daily roll-up of what kills intents.

Operator question: "which doctrine checks kill the most intents, so
thresholds can be tuned?" Aggregates `shared_intents` terminal stamps
(`gate_state` blocked/advisory_only/no_trade/expired_unrouted) over
the last N hours, grouped by `broker_reason` × lane × UTC day.

Window is capped at 72h — the retention sweeper purges non-executed
intents after 3 days, so anything older no longer exists to count.
Uses the `(gate_state, ingest_ts)` compound index.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db

router = APIRouter(prefix="/admin/gate-failure-digest", tags=["admin-gate-digest"])

_KILL_STATES = ["blocked", "advisory_only", "no_trade", "expired_unrouted"]


@router.get("")
async def gate_failure_digest(
    hours: int = Query(default=24, ge=1, le=72),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    since = (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()
    pipe = [
        {"$match": {
            "gate_state": {"$in": _KILL_STATES},
            "ingest_ts": {"$gte": since},
        }},
        {"$group": {
            "_id": {
                "reason": {"$ifNull": ["$broker_reason", "(unstamped)"]},
                "lane": {"$ifNull": ["$lane", "?"]},
                "gate_state": "$gate_state",
                "day": {"$substrBytes": ["$ingest_ts", 0, 10]},
            },
            "count": {"$sum": 1},
        }},
    ]
    rows = []
    async for d in db["shared_intents"].aggregate(pipe, maxTimeMS=8000):
        k = d["_id"]
        rows.append({
            "reason": str(k.get("reason"))[:120],
            "lane": k.get("lane"),
            "gate_state": k.get("gate_state"),
            "day": k.get("day"),
            "count": d["count"],
        })

    # Roll up: top reasons across the window + per-day totals.
    by_reason: dict[tuple, dict] = {}
    by_day: dict[str, dict] = {}
    total = 0
    for r in rows:
        total += r["count"]
        rk = (r["reason"], r["lane"], r["gate_state"])
        agg = by_reason.setdefault(rk, {
            "reason": r["reason"], "lane": r["lane"],
            "gate_state": r["gate_state"], "count": 0,
        })
        agg["count"] += r["count"]
        day = by_day.setdefault(r["day"], {"day": r["day"], "total": 0, "reasons": {}})
        day["total"] += r["count"]
        day["reasons"][r["reason"]] = day["reasons"].get(r["reason"], 0) + r["count"]

    top_reasons = sorted(by_reason.values(), key=lambda x: -x["count"])[:20]
    days = sorted(by_day.values(), key=lambda x: x["day"], reverse=True)
    for d in days:
        d["top"] = sorted(
            ({"reason": k, "count": v} for k, v in d["reasons"].items()),
            key=lambda x: -x["count"],
        )[:5]
        del d["reasons"]

    return {
        "hours": hours,
        "since": since,
        "total_killed": total,
        "top_reasons": top_reasons,
        "by_day": days,
        "note": (
            "Window capped at 72h — non-executed intents are purged by "
            "the retention sweeper after 3 days."
        ),
    }
