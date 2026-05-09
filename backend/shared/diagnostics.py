"""Diagnostics endpoints. Read-only system health + per-runtime liveness."""
import os
from datetime import datetime, timezone
from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db, client
from namespaces import (
    SHARED_RECEIPTS, SHARED_MEMORY, SHARED_HEARTBEATS,
    ALPHA_DECISION_LOG, CAMARO_SHADOW_ROWS, CHEVELLE_MEMORY_LABELS, RUNTIMES,
)


router = APIRouter(prefix="/admin/diagnostics", tags=["diagnostics"])


async def _last_receipt_ts(runtime: str) -> str | None:
    doc = await db[SHARED_RECEIPTS].find_one(
        {"runtime": runtime}, {"_id": 0, "timestamp": 1}, sort=[("timestamp", -1)]
    )
    return doc["timestamp"] if doc else None


async def _runtime_log_count(runtime: str) -> int:
    coll = {
        "alpha": ALPHA_DECISION_LOG,
        "camaro": CAMARO_SHADOW_ROWS,
        "chevelle": CHEVELLE_MEMORY_LABELS,
    }[runtime]
    return await db[coll].count_documents({})


@router.get("")
async def diagnostics(_user: dict = Depends(get_current_user)):
    try:
        await client.admin.command("ping")
        mongo_ok = True
        mongo_err = None
    except Exception as e:  # noqa: BLE001
        mongo_ok = False
        mongo_err = str(e)

    per_runtime = []
    for rt in RUNTIMES:
        hb = await db[SHARED_HEARTBEATS].find_one({"runtime": rt}, {"_id": 0})
        per_runtime.append({
            "runtime": rt,
            "last_receipt_ts": await _last_receipt_ts(rt),
            "log_count": await _runtime_log_count(rt),
            "memory_labels_count": await db[SHARED_MEMORY].count_documents({"runtime": rt}),
            "heartbeat": hb,
        })

    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "deploy_mode": os.environ.get("DEPLOY_MODE", "observation"),
        "mongo": {"ok": mongo_ok, "error": mongo_err},
        "runtimes": per_runtime,
    }
