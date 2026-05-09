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
    SHARED_HEARTBEATS, RUNTIMES,
)
from runtime_auth import verify_runtime_token


router = APIRouter(prefix="/ingest", tags=["ingest"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _broker_live_enabled() -> bool:
    return os.environ.get("BROKER_LIVE_ORDER_ENABLED", "false").lower() == "true"


# ------------------------------- Receipts -------------------------------
class ReceiptIn(BaseModel):
    runtime: Literal["alpha", "camaro", "chevelle"]
    action: str = Field(..., min_length=1, max_length=64)
    intent: dict = Field(default_factory=dict)
    executed: bool = False  # observation mode forces False on the way in


@router.post("/receipts")
async def ingest_receipt(
    body: ReceiptIn,
    x_runtime_token: str | None = Header(default=None, alias="X-Runtime-Token"),
):
    verify_runtime_token(body.runtime, x_runtime_token or "")
    # Observation invariant: even if a runtime claims executed=True, we coerce
    # to False unless BROKER_LIVE_ORDER_ENABLED is true.
    executed = bool(body.executed) and _broker_live_enabled()
    doc = {
        "id": str(uuid.uuid4()),
        "runtime": body.runtime,
        "action": body.action,
        "intent": body.intent,
        "observed": True,
        "executed": executed,
        "timestamp": _now_iso(),
    }
    await db[SHARED_RECEIPTS].insert_one(doc)
    return {"ok": True, "id": doc["id"], "executed": executed}


# ----------------------------- Memory labels -----------------------------
class MemoryLabelIn(BaseModel):
    runtime: Literal["alpha", "camaro", "chevelle"]
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
    runtime: Literal["alpha", "camaro", "chevelle"]
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
    runtime: Literal["alpha", "camaro", "chevelle"]
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
    runtime: Literal["alpha", "camaro", "chevelle"]
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
