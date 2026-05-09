"""Promotion workflow — Patent J readiness + operator-countersigned authority elevation.

Doctrine:
  Camaro proves itself
  → evidence accumulates                           (PromotionArtifact = Patent G)
  → PromotionArtifact emitted                      (POST /api/ingest/promotion-artifact)
  → Patent J readiness passes                      (this module's evaluate_readiness)
  → operator reviews                               (GET /api/admin/promotion/proposals)
  → role elevation allowed                         (POST /api/admin/promotion/{id}/countersign)

Promotion is NEVER organic. A runtime cannot promote itself.
"""
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import (
    SHARED_AUTHORITY_STATE, SHARED_PROMOTION_ARTIFACTS,
    SHARED_PROMOTION_PROPOSALS, SHARED_RECEIPTS, SHARED_MEMORY,
    SHARED_HEARTBEATS, RUNTIMES, AUTHORITY_LADDER, AUTHORITY_LEVEL,
    DEFAULT_AUTHORITY, GOVERNOR_STATE, PROMOTION_THRESHOLDS,
    is_on_ladder, next_authority,
)


router = APIRouter(prefix="/admin/promotion", tags=["promotion"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat()


# ─────────────────────────── State helpers ───────────────────────────

async def _current_state(runtime: str) -> dict:
    """Return the runtime's current authority state doc, lazy-installing default."""
    doc = await db[SHARED_AUTHORITY_STATE].find_one({"runtime": runtime}, {"_id": 0})
    if doc:
        return doc
    default_state = DEFAULT_AUTHORITY.get(runtime, "observer")
    new_doc = {
        "runtime": runtime,
        "authority_state": default_state,
        "history": [{
            "to_state": default_state, "from_state": None,
            "at": _iso(), "via": "default_install",
            "operator": None, "proposal_id": None,
        }],
        "created_at": _iso(),
    }
    await db[SHARED_AUTHORITY_STATE].update_one(
        {"runtime": runtime}, {"$setOnInsert": new_doc}, upsert=True
    )
    return new_doc


# ─────────────────────────── Patent J: readiness gate ───────────────────────────

async def evaluate_readiness(runtime: str, target_authority: str, artifact: dict | None = None) -> dict:
    """Run every Patent J check. Returns a structured verdict; pass requires
    every check to be green."""
    now = _now()
    cutoff_24h = (now - timedelta(hours=24)).isoformat()

    # Pull the latest artifact if one wasn't passed
    if artifact is None:
        artifact = await db[SHARED_PROMOTION_ARTIFACTS].find_one(
            {"runtime": runtime, "target_authority": target_authority},
            {"_id": 0}, sort=[("emitted_at", -1)],
        )

    metrics = (artifact or {}).get("metrics", {}) if artifact else {}

    ece = metrics.get("ece")
    brier = metrics.get("brier")
    resolved_rows = metrics.get("resolved_rows")
    disagreement_stability = metrics.get("disagreement_stability")
    audit_integrity_pass = metrics.get("audit_integrity_pass")

    # Live signals from the shared nervous system
    toxic_24h = await db[SHARED_MEMORY].count_documents({
        "runtime": runtime, "label": "quarantine", "timestamp": {"$gte": cutoff_24h},
    })
    role_violations_24h = await db[SHARED_RECEIPTS].count_documents({
        "runtime": runtime, "role_violation": True, "timestamp": {"$gte": cutoff_24h},
    })
    hb = await db[SHARED_HEARTBEATS].find_one({"runtime": runtime}, {"_id": 0})
    hb_age_seconds: float | None = None
    if hb:
        try:
            hb_age_seconds = (now - datetime.fromisoformat(hb["last_seen"])).total_seconds()
        except Exception:  # noqa: BLE001
            hb_age_seconds = None

    T = PROMOTION_THRESHOLDS
    checks = []

    def add(name: str, passed: bool, observed, threshold) -> None:
        checks.append({"name": name, "pass": bool(passed), "observed": observed, "threshold": threshold})

    add("artifact_present", artifact is not None, bool(artifact), True)
    add("calibration_ece", isinstance(ece, (int, float)) and ece <= T["ece_max"], ece, f"<= {T['ece_max']}")
    add("calibration_brier", isinstance(brier, (int, float)) and brier <= T["brier_max"], brier, f"<= {T['brier_max']}")
    add("resolved_rows", isinstance(resolved_rows, int) and resolved_rows >= T["min_resolved_rows"], resolved_rows, f">= {T['min_resolved_rows']}")
    add("disagreement_stability", isinstance(disagreement_stability, (int, float)) and disagreement_stability >= T["min_disagreement_stability"], disagreement_stability, f">= {T['min_disagreement_stability']}")
    add("audit_integrity", audit_integrity_pass is True, audit_integrity_pass, True)
    add("toxic_memory_24h", toxic_24h <= T["max_toxic_memory_24h"], toxic_24h, f"<= {T['max_toxic_memory_24h']}")
    add("role_violations_24h", role_violations_24h <= T["max_role_violations_24h"], role_violations_24h, f"<= {T['max_role_violations_24h']}")
    add("heartbeat_recent", hb_age_seconds is not None and hb_age_seconds <= T["heartbeat_max_age_seconds"], hb_age_seconds, f"<= {T['heartbeat_max_age_seconds']}s")

    all_pass = all(c["pass"] for c in checks)
    return {
        "runtime": runtime,
        "target_authority": target_authority,
        "evaluated_at": _iso(now),
        "passed": all_pass,
        "checks": checks,
        "artifact_id": (artifact or {}).get("artifact_id"),
        "thresholds": T,
        "note": (
            "Patent J readiness gate. PASS still requires operator countersign "
            "to elevate authority — the gate alone never promotes."
        ),
    }


# ─────────────────────────── Operator API ───────────────────────────

class CountersignBody(BaseModel):
    note: str = Field("", max_length=1024)


@router.get("/state")
async def list_authority_states(_user: dict = Depends(get_current_user)):
    """Current authority state for every runtime, with embedded history."""
    items = []
    for rt in RUNTIMES:
        items.append(await _current_state(rt))
    return {"items": items}


@router.get("/artifacts")
async def list_artifacts(
    runtime: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    _user: dict = Depends(get_current_user),
):
    q = {"runtime": runtime} if runtime else {}
    docs = await db[SHARED_PROMOTION_ARTIFACTS].find(q, {"_id": 0}).sort("emitted_at", -1).to_list(limit)
    return {"items": docs, "count": len(docs)}


@router.post("/propose")
async def propose_from_latest_artifact(
    runtime: str,
    target_authority: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
):
    """Operator-initiated: take the latest artifact for `runtime` (optionally
    constrained to a target_authority), run Patent J, and create a proposal.
    The proposal does NOT elevate; operator must countersign."""
    if runtime not in RUNTIMES:
        raise HTTPException(status_code=400, detail=f"runtime must be one of {RUNTIMES}")
    state = await _current_state(runtime)
    current = state["authority_state"]
    if current == GOVERNOR_STATE:
        raise HTTPException(status_code=400, detail="governor role is off-ladder; not promotable")
    if not target_authority:
        target_authority = next_authority(current)
        if not target_authority:
            raise HTTPException(status_code=400, detail="runtime is already at top of ladder")
    if target_authority not in AUTHORITY_LADDER or target_authority == "observer":
        raise HTTPException(status_code=400, detail="invalid target_authority")
    if AUTHORITY_LEVEL[target_authority] <= AUTHORITY_LEVEL.get(current, -1):
        raise HTTPException(status_code=400, detail="target must be a strict elevation")

    q: dict = {"runtime": runtime, "target_authority": target_authority}
    artifact = await db[SHARED_PROMOTION_ARTIFACTS].find_one(q, {"_id": 0}, sort=[("emitted_at", -1)])
    readiness = await evaluate_readiness(runtime, target_authority, artifact)

    # Dual-sign rule: elevation TO primary requires two distinct operator signatures.
    # Every other rung on the ladder remains single-sign. The countersign cannot
    # bypass a failed Patent J gate either way.
    required_signatures = 2 if target_authority == "primary" else 1

    proposal_id = str(uuid.uuid4())
    doc = {
        "proposal_id": proposal_id,
        "runtime": runtime,
        "from_state": current,
        "target_authority": target_authority,
        "readiness": readiness,
        "artifact_id": (artifact or {}).get("artifact_id"),
        # status flow:
        #   pending → (single-sign target) approved | rejected
        #   pending → (primary target, 1st sign) awaiting_second_sign → approved | rejected
        "status": "pending",
        "required_signatures": required_signatures,
        "signers": [],
        "created_at": _iso(),
        "created_by": user.get("email"),
        "decided_at": None,
        "decided_by": None,
        "decision_note": None,
    }
    await db[SHARED_PROMOTION_PROPOSALS].insert_one(doc)
    return {
        "ok": True,
        "proposal_id": proposal_id,
        "readiness_passed": readiness["passed"],
        "required_signatures": required_signatures,
    }


@router.get("/proposals")
async def list_proposals(
    status: Optional[str] = Query(None, description="pending|approved|rejected"),
    runtime: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    _user: dict = Depends(get_current_user),
):
    q: dict = {}
    if status:
        q["status"] = status
    if runtime:
        q["runtime"] = runtime
    docs = await db[SHARED_PROMOTION_PROPOSALS].find(q, {"_id": 0}).sort("created_at", -1).to_list(limit)
    return {"items": docs, "count": len(docs)}


@router.get("/readiness/{runtime}")
async def readiness_now(runtime: str, _user: dict = Depends(get_current_user)):
    """On-demand readiness check using the latest artifact, without creating a proposal."""
    if runtime not in RUNTIMES:
        raise HTTPException(status_code=400, detail=f"runtime must be one of {RUNTIMES}")
    state = await _current_state(runtime)
    current = state["authority_state"]
    target = next_authority(current)
    if not target:
        return {"runtime": runtime, "current": current, "target_authority": None,
                "passed": False, "checks": [], "note": "off-ladder or at top"}
    return await evaluate_readiness(runtime, target)


@router.post("/{proposal_id}/countersign")
async def countersign(proposal_id: str, body: CountersignBody, user: dict = Depends(get_current_user)):
    """Operator countersign — performs the actual authority elevation IF the
    proposal's readiness gate passed AND the required number of distinct operator
    signatures has been collected. The countersign cannot bypass a failed
    readiness gate; if you need to override, raise the gate thresholds in
    config and re-propose.

    Dual-sign rule: target_authority == "primary" requires two distinct operator
    signatures. The first countersign records the signer and parks the proposal
    in `awaiting_second_sign`. The second countersign (from a different operator)
    finalises the elevation. Self-double-signing is rejected.
    """
    proposal = await db[SHARED_PROMOTION_PROPOSALS].find_one({"proposal_id": proposal_id}, {"_id": 0})
    if not proposal:
        raise HTTPException(status_code=404, detail="proposal not found")
    status = proposal["status"]
    if status not in ("pending", "awaiting_second_sign"):
        raise HTTPException(status_code=409, detail=f"proposal already {status}")
    if not proposal["readiness"]["passed"]:
        raise HTTPException(status_code=412, detail="readiness gate has not passed; cannot countersign")

    runtime = proposal["runtime"]
    target = proposal["target_authority"]
    required = proposal.get("required_signatures", 1)
    signers = list(proposal.get("signers", []))
    operator_email = (user.get("email") or "").lower()

    # No operator may countersign the same proposal twice — dual-sign requires
    # two distinct human reviewers.
    if any((s.get("operator") or "").lower() == operator_email for s in signers):
        raise HTTPException(
            status_code=409,
            detail="this operator has already countersigned this proposal; a second, distinct operator is required",
        )

    new_signer = {"operator": user.get("email"), "at": _iso(), "note": body.note}
    signers.append(new_signer)

    # Re-check current state to avoid races with other elevations
    state = await _current_state(runtime)
    current = state["authority_state"]
    if AUTHORITY_LEVEL.get(target, -1) <= AUTHORITY_LEVEL.get(current, -1):
        raise HTTPException(status_code=409, detail="runtime authority has changed; please re-propose")

    # Not enough signatures yet → park as awaiting_second_sign
    if len(signers) < required:
        await db[SHARED_PROMOTION_PROPOSALS].update_one(
            {"proposal_id": proposal_id},
            {"$set": {"signers": signers, "status": "awaiting_second_sign"}},
        )
        return {
            "ok": True,
            "awaiting_more_signatures": True,
            "signed": len(signers),
            "required": required,
            "from_state": current,
            "target_authority": target,
        }

    # Required signatures collected → elevate authority
    history_entry = {
        "from_state": current, "to_state": target,
        "at": _iso(), "via": "operator_countersign",
        "operator": user.get("email"), "proposal_id": proposal_id,
        "signers": [s.get("operator") for s in signers],
    }
    await db[SHARED_AUTHORITY_STATE].update_one(
        {"runtime": runtime},
        {"$set": {"authority_state": target}, "$push": {"history": history_entry}},
    )
    await db[SHARED_PROMOTION_PROPOSALS].update_one(
        {"proposal_id": proposal_id},
        {"$set": {
            "status": "approved",
            "signers": signers,
            "decided_at": _iso(),
            "decided_by": user.get("email"),
            "decision_note": body.note,
        }},
    )
    return {
        "ok": True,
        "from_state": current,
        "to_state": target,
        "signed": len(signers),
        "required": required,
    }


@router.post("/{proposal_id}/reject")
async def reject(proposal_id: str, body: CountersignBody, user: dict = Depends(get_current_user)):
    proposal = await db[SHARED_PROMOTION_PROPOSALS].find_one({"proposal_id": proposal_id}, {"_id": 0})
    if not proposal:
        raise HTTPException(status_code=404, detail="proposal not found")
    if proposal["status"] not in ("pending", "awaiting_second_sign"):
        raise HTTPException(status_code=409, detail=f"proposal already {proposal['status']}")
    await db[SHARED_PROMOTION_PROPOSALS].update_one(
        {"proposal_id": proposal_id},
        {"$set": {
            "status": "rejected",
            "decided_at": _iso(),
            "decided_by": user.get("email"),
            "decision_note": body.note,
        }},
    )
    return {"ok": True}
