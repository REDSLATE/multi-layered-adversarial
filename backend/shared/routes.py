"""Shared infrastructure read endpoints (receipts, memory, calibrators, feature builders, artifacts)."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from db import db
from namespaces import SHARED_RECEIPTS, SHARED_MEMORY, RUNTIMES, ROLES
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
    for rt in RUNTIMES:
        receipts_count = await db[SHARED_RECEIPTS].count_documents({"runtime": rt})
        labels_count = await db[SHARED_MEMORY].count_documents({"runtime": rt})
        violation_count = await db[SHARED_RECEIPTS].count_documents({"runtime": rt, "role_violation": True})
        artifacts_list = await list_artifacts(db, rt)
        latest_artifact = artifacts_list[-1] if artifacts_list else None
        last_receipt = await db[SHARED_RECEIPTS].find_one(
            {"runtime": rt}, {"_id": 0}, sort=[("timestamp", -1)]
        )
        out.append({
            "runtime": rt,
            "role": ROLES[rt]["role"],
            "role_title": ROLES[rt]["title"],
            "role_tagline": ROLES[rt]["tagline"],
            "execution_allowed": ROLES[rt]["execution_allowed"],
            "mode": "observation",
            "receipts_count": receipts_count,
            "memory_labels_count": labels_count,
            "role_violation_count": violation_count,
            "artifact_count": len(artifacts_list),
            "latest_artifact": latest_artifact,
            "last_receipt": last_receipt,
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
