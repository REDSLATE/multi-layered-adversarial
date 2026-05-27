"""Runtime-token health endpoint — surfaces silent auth rejections.

When a brain POSTs with the wrong `X-Runtime-Token` (token mismatch,
missing header, unconfigured env var) the 401 is invisible to the
operator's MC dashboard — the intent never lands in `shared_intents`
because it's dropped before persistence. This endpoint exposes the
rejection counts MC has been collecting silently.

Use case: REDEYE was sending 21k intents and only 24 landed because
the rest 401'd against MC's `REDEYE_INGEST_TOKEN`. Operator had no
way to see this until the audit was wired.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import DISCUSSION_PARTICIPANTS


router = APIRouter(
    prefix="/admin/runtime-tokens", tags=["runtime-token-health"],
)


def _cutoff(hours: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()


@router.get("/health")
async def health(
    window_hours: int = Query(24, ge=1, le=720),
    _user: dict = Depends(get_current_user),
) -> dict:
    """Per-brain rejection counts + last failure ts, last `window_hours`."""
    cutoff = _cutoff(window_hours)
    rows = []
    for brain in DISCUSSION_PARTICIPANTS:
        # Aggregate per-reason
        pipeline = [
            {"$match": {"runtime": brain, "ts": {"$gte": cutoff}}},
            {"$group": {"_id": "$reason", "n": {"$sum": 1}}},
        ]
        per_reason = {}
        total = 0
        async for r in db["runtime_token_rejections"].aggregate(pipeline):
            per_reason[r["_id"] or "unknown"] = r["n"]
            total += r["n"]
        last = await db["runtime_token_rejections"].find_one(
            {"runtime": brain}, {"_id": 0},
            sort=[("ts", -1)],
        )
        rows.append({
            "brain": brain,
            "rejections_total": total,
            "rejections_by_reason": per_reason,
            "last_rejection_at": (last or {}).get("ts"),
            "last_rejection_reason": (last or {}).get("reason"),
            "diagnosis": _diagnose(total, per_reason),
        })
    return {
        "ok": True,
        "window_hours": window_hours,
        "rows": rows,
        "note": (
            "These are 401/503 responses to runtime-token-authenticated "
            "ingest paths. If a brain shows thousands of rejections it "
            "is sending with a misaligned token — check that brain's "
            "`<BRAIN>_INGEST_TOKEN` matches MC's environment value."
        ),
    }


def _diagnose(total: int, per_reason: dict) -> str:
    if total == 0:
        return "healthy"
    if per_reason.get("token_mismatch", 0) > 100:
        return "token_mismatch_high_volume"
    if per_reason.get("token_not_configured", 0) > 0:
        return "token_not_configured_on_mc"
    if per_reason.get("missing_header", 0) > 100:
        return "header_missing_high_volume"
    return "minor_rejections"
