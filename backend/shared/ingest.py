"""Sidecar ingest endpoints. Per-runtime token auth via X-Runtime-Token header.
These are the ONLY way a runtime writes into the shared nervous system."""
import os
import uuid
from datetime import datetime, timezone
from typing import Optional, Literal

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel, Field

from db import db
from namespaces import (
    SHARED_RECEIPTS, SHARED_MEMORY, SHARED_CALIBRATORS, SHARED_ARTIFACTS,
    SHARED_HEARTBEATS, SHARED_PROMOTION_ARTIFACTS, SHARED_AUTHORITY_STATE,
    RUNTIMES, runtime_can_execute_state, DEFAULT_AUTHORITY,
)
from runtime_auth import verify_runtime_token


router = APIRouter(prefix="/ingest", tags=["ingest"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _broker_live_enabled() -> bool:
    return os.environ.get("BROKER_LIVE_ORDER_ENABLED", "false").lower() == "true"


async def _current_authority(runtime: str) -> str:
    """Look up the runtime's current authority state, creating a default
    record if none exists. This is the single source of truth for the
    receipt-execution check."""
    doc = await db[SHARED_AUTHORITY_STATE].find_one({"runtime": runtime}, {"_id": 0})
    if doc:
        return doc["authority_state"]
    # Lazy-install default
    default_state = DEFAULT_AUTHORITY.get(runtime, "observer")
    await db[SHARED_AUTHORITY_STATE].update_one(
        {"runtime": runtime},
        {"$setOnInsert": {
            "runtime": runtime,
            "authority_state": default_state,
            "history": [{
                "to_state": default_state, "from_state": None,
                "at": _now_iso(), "via": "default_install",
                "operator": None, "proposal_id": None,
            }],
            "created_at": _now_iso(),
        }},
        upsert=True,
    )
    return default_state


# ------------------------------- Receipts -------------------------------
class ReceiptIn(BaseModel):
    runtime: Literal["alpha", "camaro", "chevelle", "redeye"]
    action: str = Field(..., min_length=1, max_length=64)
    intent: dict = Field(default_factory=dict)
    executed: bool = False  # observation mode forces False on the way in


@router.post("/receipts")
async def ingest_receipt(
    body: ReceiptIn,
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
):
    verify_runtime_token(body.runtime, x_runtime_token or "")

    # ─── Doctrine update (2026-05-14) ───
    # Authority lives on SEATS, not brains. A brain's identity does NOT
    # grant or restrict what it may do — the seat it currently holds
    # does. We still record the brain's current authority_state in the
    # receipt for audit context, but it no longer gates `executed`.
    # The actual execution gate runs in `/api/execution/submit` (and the
    # auto-router), where seat policy is the source of truth.
    authority_state = await _current_authority(body.runtime)
    executed = bool(body.executed) and _broker_live_enabled()
    role_violation = False  # legacy field, retained for schema stability

    doc = {
        "id": str(uuid.uuid4()),
        "runtime": body.runtime,
        "action": body.action,
        "intent": body.intent,
        "observed": True,
        "executed": executed,
        "role_violation": role_violation,
        "authority_state_at_emit": authority_state,
        "timestamp": _now_iso(),
    }
    await db[SHARED_RECEIPTS].insert_one(doc)
    return {
        "ok": True, "id": doc["id"],
        "executed": executed, "role_violation": role_violation,
        "authority_state": authority_state,
    }


# ----------------------------- Memory labels -----------------------------
class MemoryLabelIn(BaseModel):
    runtime: Literal["alpha", "camaro", "chevelle", "redeye"]
    label: Literal["safe", "review", "quarantine"]
    reason: str = Field("", max_length=512)
    payload_summary: str = Field("", max_length=1024)


@router.post("/memory-labels")
async def ingest_memory_label(
    body: MemoryLabelIn,
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
):
    verify_runtime_token(body.runtime, x_runtime_token or "")
    doc = {
        "id": str(uuid.uuid4()),
        "runtime": body.runtime,
        "label": body.label,
        "reason": body.reason,
        "payload_summary": body.payload_summary,
        "timestamp": _now_iso(),
    }
    await db[SHARED_MEMORY].insert_one(doc)
    return {"ok": True, "id": doc["id"]}


# ----------------------------- Calibrators -----------------------------
class CalibratorIn(BaseModel):
    runtime: Literal["alpha", "camaro", "chevelle", "redeye"]
    name: str = Field(..., min_length=1, max_length=128)
    version: str = Field(..., min_length=1, max_length=64)
    method: str = Field(..., min_length=1, max_length=64)
    fit_at: Optional[str] = None  # ISO timestamp; defaults to now


@router.post("/calibrators")
async def ingest_calibrator(
    body: CalibratorIn,
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
):
    """Idempotent register/update — keyed by (runtime, name)."""
    verify_runtime_token(body.runtime, x_runtime_token or "")
    doc = {
        "runtime": body.runtime,
        "name": body.name,
        "version": body.version,
        "method": body.method,
        "fit_at": body.fit_at or _now_iso(),
    }
    await db[SHARED_CALIBRATORS].update_one(
        {"runtime": body.runtime, "name": body.name},
        {"$set": doc},
        upsert=True,
    )
    return {"ok": True}


# ----------------------------- Artifacts -----------------------------
class ArtifactIn(BaseModel):
    runtime: Literal["alpha", "camaro", "chevelle", "redeye"]
    artifact: str = Field(..., min_length=1, max_length=128)
    version: str = Field(..., min_length=1, max_length=64)
    sha: str = Field(..., min_length=1, max_length=128)
    registered_at: Optional[str] = None


@router.post("/artifacts")
async def ingest_artifact(
    body: ArtifactIn,
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
):
    """Idempotent register/update — keyed by (runtime, artifact)."""
    verify_runtime_token(body.runtime, x_runtime_token or "")
    doc = {
        "runtime": body.runtime,
        "artifact": body.artifact,
        "version": body.version,
        "sha": body.sha,
        "registered_at": body.registered_at or _now_iso(),
    }
    await db[SHARED_ARTIFACTS].update_one(
        {"runtime": body.runtime, "artifact": body.artifact},
        {"$set": doc},
        upsert=True,
    )
    return {"ok": True}


# ----------------------------- Heartbeat -----------------------------
class HeartbeatIn(BaseModel):
    runtime: Literal["alpha", "camaro", "chevelle", "redeye"]
    status: str = Field("ok", max_length=32)
    detail: dict = Field(default_factory=dict)


@router.post("/heartbeat")
async def ingest_heartbeat(
    body: HeartbeatIn,
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
):
    verify_runtime_token(body.runtime, x_runtime_token or "")
    doc = {
        "runtime": body.runtime,
        "status": body.status,
        "detail": body.detail,
        "last_seen": _now_iso(),
    }
    await db[SHARED_HEARTBEATS].update_one(
        {"runtime": body.runtime}, {"$set": doc}, upsert=True
    )
    return {"ok": True, "last_seen": doc["last_seen"]}


# ----------------------------- Promotion artifact -----------------------------
class PromotionEvidenceIn(BaseModel):
    """Patent G evidence packet — runtime declares it believes it has met
    the bar for an authority elevation. Server stores it and (if it passes
    Patent J) creates an operator proposal. Promotion never happens
    automatically — operator countersign required."""
    runtime: Literal["alpha", "camaro", "chevelle", "redeye"]
    target_authority: Literal["challenger", "advisor", "co_trader", "primary"]
    metrics: dict = Field(..., description=(
        "Required keys: ece (float), brier (float), resolved_rows (int), "
        "disagreement_stability (float), audit_integrity_pass (bool). "
        "Optional: any additional evidence the runtime wants to attach."
    ))
    notes: str = Field("", max_length=2048)


@router.post("/promotion-artifact")
async def ingest_promotion_artifact(
    body: PromotionEvidenceIn,
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
):
    """Runtime emits a PromotionArtifact to claim it has met the bar for
    an authority elevation. This DOES NOT change the authority state —
    that requires Patent J pass + operator countersign (see /api/admin/promotion)."""
    verify_runtime_token(body.runtime, x_runtime_token or "")
    artifact_id = str(uuid.uuid4())
    doc = {
        "artifact_id": artifact_id,
        "runtime": body.runtime,
        "target_authority": body.target_authority,
        "metrics": body.metrics,
        "notes": body.notes,
        "emitted_at": _now_iso(),
    }
    await db[SHARED_PROMOTION_ARTIFACTS].insert_one(doc)
    return {"ok": True, "artifact_id": artifact_id}
