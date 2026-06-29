"""Direct-Execute admin + diagnostic endpoints.

Operator-facing surface for the `DIRECT_EXECUTE_MODE` fast path:

    GET  /api/admin/direct-execute/status         current flag + counts
    POST /api/admin/direct-execute/toggle         flip at runtime
    GET  /api/admin/direct-execute/recent         last N attempts
    POST /api/admin/direct-execute/replay         re-fire one intent (debug)

The "recent" endpoint is the one the operator uses to answer
"what did Webull actually say to my order?" — it surfaces the raw
broker exception type + message captured by `direct_execute(...)`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import SHARED_GATE_RESULTS, SHARED_INTENTS
from shared.direct_execute import (
    direct_execute,
    is_direct_execute_enabled,
    set_direct_execute_enabled,
)


logger = logging.getLogger("risedual.direct_execute_admin")
router = APIRouter(prefix="/admin/direct-execute", tags=["direct-execute"])


_AUDIT_KINDS = (
    "direct_execute_submitted",
    "direct_execute_blocked",
    "direct_execute_failed",
    "direct_execute_skipped",
)


@router.get("/status")
async def status(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Current flag state + last-24h counts by outcome."""
    enabled = await is_direct_execute_enabled()
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    pipeline = [
        {"$match": {"kind": {"$in": list(_AUDIT_KINDS)}, "ts": {"$gte": since}}},
        {"$group": {"_id": "$kind", "n": {"$sum": 1}}},
    ]
    counts = {k: 0 for k in _AUDIT_KINDS}
    async for row in db[SHARED_GATE_RESULTS].aggregate(
        pipeline, maxTimeMS=10_000,
    ):
        counts[row["_id"]] = int(row["n"])
    return {
        "enabled": enabled,
        "doctrine_note": (
            "Direct-execute mode bypasses dry-run, soft gates, and the "
            "auto-submit policy filter. Money safety (per-order cap, "
            "freeze, broker connection, Webull cap evaluator) still "
            "applies."
        ),
        "last_24h_counts": counts,
        "last_24h_total": sum(counts.values()),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


class ToggleIn(BaseModel):
    enabled: bool = Field(..., description="true → direct execute ON; false → OFF")
    confirm: str = Field(
        default="",
        description="must equal 'direct_execute' to take effect",
    )


@router.post("/toggle")
async def toggle(body: ToggleIn, user: dict = Depends(get_current_user)):  # noqa: B008
    """Flip the runtime override. Confirmation phrase required to
    avoid accidental hot-flip during incident response."""
    if body.confirm != "direct_execute":
        raise HTTPException(
            status_code=400,
            detail="confirmation phrase missing — set confirm='direct_execute'",
        )
    actor = user.get("email") or "operator"
    state = await set_direct_execute_enabled(body.enabled, actor)
    logger.warning(
        "DIRECT_EXECUTE_MODE flipped: enabled=%s by=%s",
        body.enabled, actor,
    )
    return {"ok": True, **state}


@router.get("/recent")
async def recent(
    limit: int = Query(default=50, ge=1, le=500),
    kind: Optional[str] = Query(
        default=None,
        description="filter to one kind: submitted | blocked | failed | skipped",
    ),
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Return the most recent direct-execute attempts with broker
    response or exception. This is the diagnostic the operator hits
    when trades aren't firing — it shows EXACTLY what the broker
    returned (or the raw Python exception if it crashed)."""
    q: dict = {"kind": {"$in": list(_AUDIT_KINDS)}}
    if kind:
        q["kind"] = f"direct_execute_{kind}"
    rows = (
        await db[SHARED_GATE_RESULTS]
        .find(
            q,
            {"_id": 0},
        )
        .sort("ts", -1)
        .max_time_ms(15_000)
        .to_list(limit)
    )
    # Group counts so the operator gets a fast summary at the top.
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.get("kind", "?")] = counts.get(r.get("kind", "?"), 0) + 1
    return {
        "count": len(rows),
        "summary_by_kind": counts,
        "items": rows,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


class ReplayIn(BaseModel):
    intent_id: str = Field(..., min_length=8, max_length=80)
    notional_usd: Optional[float] = Field(
        default=None,
        description="override per-order notional (defaults to env)",
    )


@router.post("/replay")
async def replay(
    body: ReplayIn,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Re-fire a single intent through the direct-execute path.
    Useful for debugging: pick an intent_id from `/recent`, replay
    it after fixing the underlying broker issue."""
    actor = f"direct_execute_replay:{user.get('email', 'operator')}"
    # Reset executed flag so the idempotency check doesn't refuse;
    # this is an explicit operator-driven replay.
    await db[SHARED_INTENTS].update_one(
        {"intent_id": body.intent_id},
        {"$unset": {"executed": "", "executed_at": "", "executed_by": ""}},
    )
    result = await direct_execute(
        body.intent_id,
        actor=actor,
        notional_usd=body.notional_usd,
    )
    return {"ok": result.get("verdict") == "submitted", **result}
