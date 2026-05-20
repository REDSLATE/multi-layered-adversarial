"""
Memory Kernel HTTP routes — P0
==============================

Mount path: this module exposes `router` with prefix `/admin/memory-kernel`.
It is mounted under `api_router` (which adds `/api`), so the live paths are:

    POST /api/admin/memory-kernel/submit
    POST /api/admin/memory-kernel/route
    POST /api/admin/memory-kernel/trainable/fetch-lock
    POST /api/admin/memory-kernel/trainable/confirm
    GET  /api/admin/memory-kernel/health

The submit endpoint runs the BrainMemoryTranslator first so brains may
speak in their own dialect and MC still stores exactly one language.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from services.brain_memory_translator import translate_brain_memory
from services.memory_kernel import KernelGate, MemoryKernelLedger


router = APIRouter(prefix="/admin/memory-kernel", tags=["memory-kernel"])


# ───── request models ────────────────────────────────────────────────


class SubmitMemoryRequest(BaseModel):
    source_stack: str = Field(..., examples=["redeye", "camaro", "alpha", "chevelle"])
    memory_type: str = Field(..., examples=["execution", "diagnostic", "replay"])
    payload: Dict[str, Any]
    requested_provenance: Optional[str] = None


class RouteRequest(BaseModel):
    memory_id: str
    from_component: str
    to_component: str


class FetchTrainableRequest(BaseModel):
    min_samples: int = 20
    limit: int = 200


class ConfirmTrainingRequest(BaseModel):
    memory_ids: List[str]
    lock_id: str


# ───── response sanitiser ────────────────────────────────────────────


def _strip_mongo_id(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if isinstance(doc, dict):
        doc.pop("_id", None)
    return doc


# ───── endpoints ─────────────────────────────────────────────────────


@router.post("/submit")
async def submit_memory(
    req: SubmitMemoryRequest,
    _user: dict = Depends(get_current_user),
):
    """
    Brain → Translator → Ledger.

    Many brain dialects in, exactly one MC language out, then MC classifies
    provenance. Stacks may not self-certify VE; they may only request it.
    """
    canonical_stack, canonical_type, canonical_payload = translate_brain_memory(
        source_stack=req.source_stack,
        memory_type=req.memory_type,
        payload=req.payload,
    )

    ledger = MemoryKernelLedger(db)
    doc = await ledger.submit_memory(
        source_stack=canonical_stack,
        memory_type=canonical_type,
        payload=canonical_payload,
        requested_provenance=req.requested_provenance,
    )
    return _strip_mongo_id(doc)


@router.post("/route")
async def route_memory(
    req: RouteRequest,
    _user: dict = Depends(get_current_user),
):
    gate = KernelGate(db)
    out = await gate.route(
        memory_id=req.memory_id,
        from_component=req.from_component,
        to_component=req.to_component,
    )
    _strip_mongo_id(out.get("route"))
    return out


@router.post("/trainable/fetch-lock")
async def fetch_trainable(
    req: FetchTrainableRequest,
    _user: dict = Depends(get_current_user),
):
    ledger = MemoryKernelLedger(db)
    out = await ledger.fetch_and_lock_trainable(
        min_samples=req.min_samples,
        limit=req.limit,
    )
    for m in out.get("memories", []):
        _strip_mongo_id(m)
    return out


@router.post("/trainable/confirm")
async def confirm_training(
    req: ConfirmTrainingRequest,
    _user: dict = Depends(get_current_user),
):
    ledger = MemoryKernelLedger(db)
    try:
        return await ledger.confirm_training_complete(
            memory_ids=req.memory_ids,
            lock_id=req.lock_id,
        )
    except RuntimeError as e:
        # The axiom tripped. Surface as a 422 — caller violated the contract.
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/health")
async def health():
    return {
        "ok": True,
        "service": "memory_kernel",
        "p0_only": True,
        "mc_classifies_provenance": True,
        "shelly_cannot_self_certify_ve": True,
    }
