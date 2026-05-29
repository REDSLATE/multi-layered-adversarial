"""Diagnostics endpoints. Read-only system health + per-runtime liveness."""
import os
from datetime import datetime, timezone
from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db, client
from namespaces import (
    SHARED_RECEIPTS, SHARED_MEMORY, SHARED_HEARTBEATS,
    ALPHA_DECISION_LOG, CAMARO_SHADOW_ROWS, CHEVELLE_MEMORY_LABELS,
    REDEYE_DECISION_LOG, RUNTIMES,
    SHARED_OPINIONS,
    HEARTBEAT_STALE_AFTER_SECONDS,
    HEARTBEAT_OK_BELOW_SECONDS,
    HEARTBEAT_PREVIEW_DRIFT_SECONDS,
)


def _heartbeat_tier(age: float | None) -> str:
    """Liveness band ONLY — derived purely from heartbeat age.

    Doctrine (2026-02-18): this function used to return a
    `preview_drift` tier that conflated "stale heartbeat" with "wrong
    MC URL". That heuristic produced false alarms whenever a brain
    did real LLM work that exceeded the 110s window — operators spent
    cycles chasing phantom MC_BASE_URL misconfiguration. The actual
    "is this pod on preview?" verdict comes from
    `sidecar_checkin._verdict_from_validation`, which inspects the
    brain's stamped `env_name` + `mc_url`. THIS function answers only:
    "how long since the brain last said hello?".

    Bands:
        ok            < HEARTBEAT_OK_BELOW_SECONDS      (healthy)
        stale         < HEARTBEAT_PREVIEW_DRIFT_SECONDS (slow ping)
        dead          ≥ HEARTBEAT_PREVIEW_DRIFT_SECONDS (no recent ping)
        unknown       no heartbeat ever recorded
    """
    if age is None:
        return "unknown"
    if age < HEARTBEAT_OK_BELOW_SECONDS:
        return "ok"
    if age < HEARTBEAT_PREVIEW_DRIFT_SECONDS:
        return "stale"
    return "dead"


router = APIRouter(prefix="/admin/diagnostics", tags=["diagnostics"])


async def _last_receipt_ts(runtime: str) -> str | None:
    doc = await db[SHARED_RECEIPTS].find_one(
        {"runtime": runtime}, {"_id": 0, "timestamp": 1}, sort=[("timestamp", -1)]
    )
    return doc["timestamp"] if doc else None


async def _runtime_log_count(runtime: str) -> int:
    """Per-brain canonical decision-log count.

    2026-05-29: REDEYE now has its own `redeye_decision_log` collection
    (parity with alpha/camaro/chevelle). MC reads from it directly so
    the column shows TRUE intent count instead of falling back to the
    opinion-post count it was using before. Contract for the RedEye
    team: see /app/memory/MC_HANDOFF_redeye_decision_log.md.

    If RedEye's log doesn't exist yet (brand-new pod, or stamp not
    arrived), the count returns 0 instead of crashing.
    """
    coll = {
        "alpha":    ALPHA_DECISION_LOG,
        "camaro":   CAMARO_SHADOW_ROWS,
        "chevelle": CHEVELLE_MEMORY_LABELS,
        "redeye":   REDEYE_DECISION_LOG,
    }.get(runtime)
    if coll is None:
        # Unknown runtime — opinion-post count as the safe fallback.
        return await db[SHARED_OPINIONS].count_documents({"runtime": runtime})
    return await db[coll].count_documents({})


def _hb_age_and_stale(hb: dict | None) -> tuple[float | None, bool]:
    """Compute heartbeat age in seconds and whether it's stale."""
    if not hb or not hb.get("last_seen"):
        return None, True
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(hb["last_seen"])).total_seconds()
    except Exception:  # noqa: BLE001
        return None, True
    return age, age > HEARTBEAT_STALE_AFTER_SECONDS


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
        hb_age, hb_stale = _hb_age_and_stale(hb)
        per_runtime.append({
            "runtime": rt,
            "last_receipt_ts": await _last_receipt_ts(rt),
            "log_count": await _runtime_log_count(rt),
            "memory_labels_count": await db[SHARED_MEMORY].count_documents({"runtime": rt}),
            "heartbeat": hb,
            "heartbeat_age_seconds": hb_age,
            "heartbeat_stale": hb_stale,
            "heartbeat_tier": _heartbeat_tier(hb_age),
        })

    # Lane execution toggles — the operator's real kill switch.
    # Surface alongside `deploy_mode` so the UI can stop misleading
    # the operator with an env-var-only banner.
    from shared.lane_execution import get_toggles as _lane_toggles  # noqa: WPS433
    lane_toggles = await _lane_toggles()

    return {
        "now": datetime.now(timezone.utc).isoformat(),
        "deploy_mode": os.environ.get("DEPLOY_MODE", "observation"),
        "lane_execution": {
            "equity": lane_toggles["equity"],
            "crypto": lane_toggles["crypto"],
            "any_enabled": lane_toggles["equity"] or lane_toggles["crypto"],
        },
        "heartbeat_stale_after_seconds": HEARTBEAT_STALE_AFTER_SECONDS,
        "heartbeat_ok_below_seconds": HEARTBEAT_OK_BELOW_SECONDS,
        "heartbeat_preview_drift_seconds": HEARTBEAT_PREVIEW_DRIFT_SECONDS,
        "mongo": {"ok": mongo_ok, "error": mongo_err},
        "runtimes": per_runtime,
    }
