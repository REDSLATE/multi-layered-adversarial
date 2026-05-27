"""Shared infrastructure read endpoints (receipts, memory, calibrators, feature builders, artifacts)."""
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from db import db
from namespaces import (
    SHARED_RECEIPTS, SHARED_MEMORY, SHARED_HEARTBEATS, SHARED_PROMOTION_ARTIFACTS,
    RUNTIMES, ROLES, HEARTBEAT_STALE_AFTER_SECONDS,
)
from shared.calibration_layer import list_calibrators
from shared.feature_builders import list_feature_builders
from shared.artifact_inventory import list_artifacts


router = APIRouter(prefix="/shared", tags=["shared"])


@router.get("/receipts")
async def receipts(
    runtime: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    _user: dict = Depends(get_current_user),
):
    if runtime and runtime not in RUNTIMES:
        raise HTTPException(status_code=400, detail=f"runtime must be one of {RUNTIMES}")
    q = {"runtime": runtime} if runtime else {}
    docs = await db[SHARED_RECEIPTS].find(q, {"_id": 0}).sort("timestamp", -1).to_list(limit)
    return {"items": docs, "count": len(docs)}


@router.get("/memory-labels")
async def memory_labels(
    runtime: Optional[str] = Query(None),
    label: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    _user: dict = Depends(get_current_user),
):
    if runtime and runtime not in RUNTIMES:
        raise HTTPException(status_code=400, detail=f"runtime must be one of {RUNTIMES}")
    q: dict = {}
    if runtime:
        q["runtime"] = runtime
    if label:
        q["label"] = label
    docs = await db[SHARED_MEMORY].find(q, {"_id": 0}).sort("timestamp", -1).to_list(limit)
    return {"items": docs, "count": len(docs)}


@router.get("/calibrators")
async def calibrators(
    runtime: Optional[str] = Query(None),
    _user: dict = Depends(get_current_user),
):
    return {"items": await list_calibrators(db, runtime)}


@router.get("/feature-builders")
async def feature_builders(_user: dict = Depends(get_current_user)):
    return {"items": await list_feature_builders(db)}


@router.get("/artifacts")
async def artifacts(
    runtime: Optional[str] = Query(None),
    _user: dict = Depends(get_current_user),
):
    return {"items": await list_artifacts(db, runtime)}


@router.get("/overview")
async def overview(_user: dict = Depends(get_current_user)):
    """Mission-control overview: per-runtime summary card data."""
    out = []
    violation_total = await db[SHARED_RECEIPTS].count_documents({"role_violation": True})
    now = datetime.now(timezone.utc)
    for rt in RUNTIMES:
        receipts_count = await db[SHARED_RECEIPTS].count_documents({"runtime": rt})
        labels_count = await db[SHARED_MEMORY].count_documents({"runtime": rt})
        violation_count = await db[SHARED_RECEIPTS].count_documents({"runtime": rt, "role_violation": True})
        artifacts_list = await list_artifacts(db, rt)
        latest_artifact = artifacts_list[-1] if artifacts_list else None
        last_receipt = await db[SHARED_RECEIPTS].find_one(
            {"runtime": rt}, {"_id": 0}, sort=[("timestamp", -1)]
        )
        # Authority state — informational metadata only (2026-05-26
        # doctrine collapse). The lock is now SEAT POLICY + KILL SWITCH:
        #   * `execution_allowed` = is this runtime currently sitting
        #     in a seat whose `may_execute=True` policy? (Seat-based,
        #     not identity-based.)
        #   * The authority_state field is kept for historical
        #     continuity but no longer gates anything.
        state_doc = await db["shared_authority_state"].find_one({"runtime": rt}, {"_id": 0})
        authority_state = state_doc["authority_state"] if state_doc else "observer"

        # Seat-based execution permission. Look up the current roster
        # assignment and ask seat_policy whether THAT seat may execute.
        execution_allowed = False
        seat_name = None
        try:
            from shared.roster import get_roster  # noqa: WPS433
            from shared.seat_policy import SEAT_POLICY  # noqa: WPS433
            roster = await get_roster()
            assignments = (roster or {}).get("assignments") or {}
            for seat, occupant in assignments.items():
                if occupant == rt:
                    seat_name = seat
                    pol = SEAT_POLICY.get(seat) or {}
                    if pol.get("may_execute") is True:
                        execution_allowed = True
                        break
        except Exception:  # noqa: BLE001
            # Fail-CLOSED: if seat policy can't be consulted, no execution.
            execution_allowed = False
            seat_name = None

        # Heartbeat staleness — visibility only
        hb = await db[SHARED_HEARTBEATS].find_one({"runtime": rt}, {"_id": 0})
        hb_age = None
        if hb and hb.get("last_seen"):
            try:
                hb_age = (now - datetime.fromisoformat(hb["last_seen"])).total_seconds()
            except Exception:  # noqa: BLE001
                hb_age = None
        hb_stale = hb_age is None or hb_age > HEARTBEAT_STALE_AFTER_SECONDS

        out.append({
            "runtime": rt,
            "role": ROLES[rt]["role"],
            "role_title": ROLES[rt]["title"],
            "role_tagline": ROLES[rt]["tagline"],
            "authority_state": authority_state,
            "execution_allowed": execution_allowed,
            "current_seat": seat_name,
            "mode": "observation",
            "receipts_count": receipts_count,
            "memory_labels_count": labels_count,
            "role_violation_count": violation_count,
            "artifact_count": len(artifacts_list),
            "latest_artifact": latest_artifact,
            "last_receipt": last_receipt,
            "heartbeat_age_seconds": hb_age,
            "heartbeat_stale": hb_stale,
        })
    return {"runtimes": out, "role_violation_total": violation_total}


@router.get("/role-violations")
async def role_violations(
    limit: int = Query(50, ge=1, le=500),
    _user: dict = Depends(get_current_user),
):
    """Receipts where a non-Trader runtime attempted executed=true.
    Populated automatically by the ingest layer."""
    docs = await db[SHARED_RECEIPTS].find(
        {"role_violation": True}, {"_id": 0}
    ).sort("timestamp", -1).to_list(limit)
    return {"items": docs, "count": len(docs)}


@router.get("/recent-ingests")
async def recent_ingests(
    limit: int = Query(80, ge=1, le=200),
    _user: dict = Depends(get_current_user),
):
    """Unified, time-sorted stream of the last N events across receipts,
    memory labels, and promotion artifacts. Cheap polling endpoint for the
    dashboard's live tail. Visibility-only — no state mutation."""
    receipts = await db[SHARED_RECEIPTS].find(
        {}, {"_id": 0, "id": 1, "runtime": 1, "action": 1, "intent": 1,
             "executed": 1, "role_violation": 1, "timestamp": 1,
             "authority_state_at_emit": 1}
    ).sort("timestamp", -1).to_list(limit)

    labels = await db[SHARED_MEMORY].find(
        {}, {"_id": 0, "id": 1, "runtime": 1, "label": 1, "reason": 1,
             "payload_summary": 1, "timestamp": 1}
    ).sort("timestamp", -1).to_list(limit)

    artifacts = await db[SHARED_PROMOTION_ARTIFACTS].find(
        {}, {"_id": 0, "artifact_id": 1, "runtime": 1, "target_authority": 1,
             "metrics": 1, "notes": 1, "emitted_at": 1}
    ).sort("emitted_at", -1).to_list(limit)

    events: list[dict] = []
    for r in receipts:
        events.append({"kind": "receipt", "ts": r.get("timestamp"), **r})
    for ml in labels:
        events.append({"kind": "memory_label", "ts": ml.get("timestamp"), **ml})
    for a in artifacts:
        events.append({"kind": "promotion_artifact", "ts": a.get("emitted_at"), **a})
    events.sort(key=lambda e: e.get("ts") or "", reverse=True)
    return {"items": events[:limit], "count": min(limit, len(events))}
