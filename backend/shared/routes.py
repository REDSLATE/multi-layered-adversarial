"""Shared infrastructure read endpoints (receipts, memory, calibrators, feature builders, artifacts)."""
import asyncio
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from db import db
from namespaces import (
    SHARED_RECEIPTS, SHARED_MEMORY, SHARED_HEARTBEATS, SHARED_PROMOTION_ARTIFACTS,
    RUNTIMES, ROLES, HEARTBEAT_STALE_AFTER_SECONDS, SHARED_INTENTS,
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


_OVERVIEW_QUERY_MAX_MS = 1500  # per-DB-call ceiling
# The MC Pulse ticks every ~30s; a 5-minute gap means the in-process
# orchestrator is genuinely down (2026-07-23 pulse-heartbeat rewire).
PULSE_STALE_AFTER_SECONDS = 300.0


def _safe_int(v, default=0):
    return v if isinstance(v, int) else default


async def _overview_for_runtime(
    rt: str, roster_assignments: dict, seat_policy: dict, now: datetime,
    pulse_latest_at: Optional[str] = None,
) -> dict:
    """Build one runtime's overview card.

    Doctrine pin (2026-07-16, prod-hang triage):
    The 4 per-runtime blocks now run under `asyncio.gather` at the
    caller — previously this was a for-loop with 8 sequential
    awaits × 4 runtimes = ~33 round-trips per request. Under Atlas
    load that dragged /shared/overview past the frontend's 25s
    ceiling and blanked the dashboard. Inside this function, the
    5 independent DB calls also run in parallel via `gather`, and
    every call has `maxTimeMS=1500` so no single slow scan can
    saturate the endpoint. Failures degrade to safe defaults (0 /
    None) — the operator sees the working brains even when one
    query trips its budget.
    """
    receipts_count, labels_count, violation_count, last_receipt, hb, artifacts_list, state_doc, last_intent = await asyncio.gather(
        db[SHARED_RECEIPTS].count_documents(
            {"runtime": rt}, maxTimeMS=_OVERVIEW_QUERY_MAX_MS,
        ),
        db[SHARED_MEMORY].count_documents(
            {"runtime": rt}, maxTimeMS=_OVERVIEW_QUERY_MAX_MS,
        ),
        db[SHARED_RECEIPTS].count_documents(
            {"runtime": rt, "role_violation": True}, maxTimeMS=_OVERVIEW_QUERY_MAX_MS,
        ),
        db[SHARED_RECEIPTS].find_one(
            {"runtime": rt}, {"_id": 0},
            sort=[("timestamp", -1)],
            max_time_ms=_OVERVIEW_QUERY_MAX_MS,
        ),
        db[SHARED_HEARTBEATS].find_one(
            {"runtime": rt}, {"_id": 0},
            max_time_ms=_OVERVIEW_QUERY_MAX_MS,
        ),
        list_artifacts(db, rt),
        db["shared_authority_state"].find_one(
            {"runtime": rt}, {"_id": 0},
            max_time_ms=_OVERVIEW_QUERY_MAX_MS,
        ),
        # Pulse-era "last signal": the sidecar receipts stream froze
        # at decommission (2026-07-21); a brain's real activity is its
        # intent emissions through the in-process pulse.
        db[SHARED_INTENTS].find_one(
            {"stack": rt}, {"_id": 0, "ingest_ts": 1},
            sort=[("ingest_ts", -1)],
            max_time_ms=_OVERVIEW_QUERY_MAX_MS,
        ),
        return_exceptions=True,
    )
    # Coerce exceptions to safe defaults — one bad query does NOT
    # blank the whole card.
    receipts_count   = _safe_int(receipts_count)
    labels_count     = _safe_int(labels_count)
    violation_count  = _safe_int(violation_count)
    if isinstance(last_receipt, Exception):
        last_receipt = None
    if isinstance(hb, Exception):
        hb = None
    if isinstance(artifacts_list, Exception):
        artifacts_list = []
    if isinstance(state_doc, Exception):
        state_doc = None
    if isinstance(last_intent, Exception):
        last_intent = None

    authority_state = state_doc["authority_state"] if state_doc else "observer"
    latest_artifact = artifacts_list[-1] if artifacts_list else None

    # Seat-based execution permission. Look up the current roster
    # assignment and ask seat_policy whether THAT seat may execute.
    execution_allowed = False
    seat_name = None
    for seat, occupant in roster_assignments.items():
        if occupant == rt:
            seat_name = seat
            pol = seat_policy.get(seat) or {}
            if pol.get("may_execute") is True:
                execution_allowed = True
                break

    # Heartbeat staleness — visibility only. Sidecar pods were retired
    # 2026-07-21 (pulse-only doctrine); their check-ins froze forever,
    # so the card heartbeat for pulse-run brains now reads MC Pulse
    # liveness instead (2026-07-23 fix: cards showed STALE — ~173000s
    # in prod after deploy despite the pulse ticking every 30s).
    from shared.runtime.sidecar_checkin import DECOMMISSIONED_SIDECARS  # noqa: WPS433
    hb_age = None
    heartbeat_source = "sidecar"
    if rt in DECOMMISSIONED_SIDECARS and pulse_latest_at:
        heartbeat_source = "mc_pulse"
        try:
            hb_age = (
                now - datetime.fromisoformat(str(pulse_latest_at))
            ).total_seconds()
        except Exception:  # noqa: BLE001
            hb_age = None
        hb_stale = hb_age is None or hb_age > PULSE_STALE_AFTER_SECONDS
    else:
        if hb and hb.get("last_seen"):
            try:
                hb_age = (now - datetime.fromisoformat(hb["last_seen"])).total_seconds()
            except Exception:  # noqa: BLE001
                hb_age = None
        hb_stale = hb_age is None or hb_age > HEARTBEAT_STALE_AFTER_SECONDS

    # Freshest of legacy receipt stream vs pulse-era intent emissions.
    last_signal_ts: Optional[str] = None
    if last_receipt and last_receipt.get("timestamp"):
        last_signal_ts = str(last_receipt["timestamp"])
    li_ts = (last_intent or {}).get("ingest_ts")
    if li_ts and (last_signal_ts is None or str(li_ts) > last_signal_ts):
        last_signal_ts = str(li_ts)

    return {
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
        "last_signal_ts": last_signal_ts,
        "heartbeat_age_seconds": hb_age,
        "heartbeat_stale": hb_stale,
        "heartbeat_source": heartbeat_source,
    }


@router.get("/overview")
async def overview(_user: dict = Depends(get_current_user)):
    """Mission-control overview: per-runtime summary card data."""
    now = datetime.now(timezone.utc)

    # Roster + seat policy are shared inputs — fetch once, not per-runtime.
    roster_assignments: dict = {}
    seat_policy: dict = {}
    try:
        from shared.roster import get_roster  # noqa: WPS433
        from shared.seat_policy import SEAT_POLICY  # noqa: WPS433
        roster = await get_roster()
        roster_assignments = (roster or {}).get("assignments") or {}
        seat_policy = SEAT_POLICY or {}
    except Exception:  # noqa: BLE001
        # Fail-CLOSED downstream: no seat lookup = no execution_allowed.
        roster_assignments = {}
        seat_policy = {}

    # Global aggregate — bounded so it can't drag the whole endpoint.
    try:
        violation_total = await db[SHARED_RECEIPTS].count_documents(
            {"role_violation": True}, maxTimeMS=_OVERVIEW_QUERY_MAX_MS,
        )
    except Exception:  # noqa: BLE001
        violation_total = 0

    # Pulse liveness — one single-doc read shared by all 4 cards
    # (mirrored by `mc_pulse.receipt.persist_receipt` on every tick).
    pulse_latest_at = None
    try:
        stack = await db["brain_runtime_metrics"].find_one(
            {"_id": "risedual_stack"}, {"pulse.latest_at": 1},
            max_time_ms=_OVERVIEW_QUERY_MAX_MS,
        )
        pulse_latest_at = ((stack or {}).get("pulse") or {}).get("latest_at")
    except Exception:  # noqa: BLE001
        pulse_latest_at = None

    # All 4 runtime cards in parallel — bounded worst-case wall clock.
    out = await asyncio.gather(*[
        _overview_for_runtime(
            rt, roster_assignments, seat_policy, now,
            pulse_latest_at=pulse_latest_at,
        )
        for rt in RUNTIMES
    ])
    return {"runtimes": list(out), "role_violation_total": violation_total}


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
