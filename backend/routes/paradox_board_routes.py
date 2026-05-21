"""Paradox board — read-only summary surface for `/admin/paradox`.

Doctrine pin (2026-02-XX):
    READ-ONLY. This module surfaces what the Paradox Coordinator
    has produced — candidates, evaluations, risk-blocks. It does
    NOT mutate anything, does NOT submit intents, does NOT route
    to broker, does NOT import any execution surface.

    Tripwire `test_paradox_board_module_no_execution_surface`
    enforces this at the source level.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import (
    LLM_CALLS,
    PARADOX_CANDIDATES,
    PARADOX_RECORDS,
)


router = APIRouter(prefix="/admin/paradox", tags=["paradox-board"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _strip(doc: Dict[str, Any]) -> Dict[str, Any]:
    doc.pop("_id", None)
    for k in ("created_at", "evaluated_at"):
        v = doc.get(k)
        if isinstance(v, datetime):
            doc[k] = v.isoformat()
    return doc


@router.get("/board")
async def get_board(
    hours: int = Query(default=24, ge=1, le=168),
    limit_per_section: int = Query(default=50, ge=1, le=200),
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Single-shot read of all four sections so the UI doesn't have
    to make four round-trips."""
    since = _now() - timedelta(hours=hours)
    since_iso = since.isoformat()

    # ── Candidates: status ∈ {candidate, pending_snapshot}
    candidates: List[Dict[str, Any]] = []
    async for d in (
        db[PARADOX_CANDIDATES]
        .find(
            {"status": {"$in": ["candidate", "pending_snapshot"]},
             "created_at": {"$gte": since}},
            {"_id": 0, "evaluation_id": 0},  # candidates not yet evaluated
        )
        .sort("created_at", -1).limit(limit_per_section)
    ):
        candidates.append(_strip(d))

    # ── Evaluations: paradox_records of kind paradox_v0_evaluation
    evaluations: List[Dict[str, Any]] = []
    async for d in (
        db[PARADOX_RECORDS]
        .find(
            {"evaluation_kind": "paradox_v0_evaluation",
             "created_at": {"$gte": since}},
        )
        .sort("created_at", -1).limit(limit_per_section)
    ):
        evaluations.append(_strip(d))

    # ── Risk-blocked: candidates flipped to risk_blocked + the
    # corresponding paradox_v0_risk_block records.
    risk_candidates: List[Dict[str, Any]] = []
    async for d in (
        db[PARADOX_CANDIDATES]
        .find({"status": "risk_blocked"})
        .sort("created_at", -1).limit(limit_per_section)
    ):
        risk_candidates.append(_strip(d))

    risk_records: List[Dict[str, Any]] = []
    async for d in (
        db[PARADOX_RECORDS]
        .find({"evaluation_kind": "paradox_v0_risk_block",
               "created_at": {"$gte": since}})
        .sort("created_at", -1).limit(limit_per_section)
    ):
        risk_records.append(_strip(d))

    # ── Ready-for-human-review: subset of evaluations whose verdict
    # is promotable. The UI surfaces these prominently because they
    # are the only ones the operator can act on.
    ready_for_review = [
        e for e in evaluations
        if e.get("verdict", {}).get("status") == "ready_for_human_review"
        and e.get("verdict", {}).get("promotable") is True
    ]

    # Counts by status so the UI can show overview pills.
    counts = {
        "candidates": len(candidates),
        "evaluations": len(evaluations),
        "risk_blocked": len(risk_candidates),
        "ready_for_human_review": len(ready_for_review),
    }

    return {
        "ok": True,
        "since": since_iso,
        "now": _now().isoformat(),
        "counts": counts,
        "candidates": candidates,
        "evaluations": evaluations,
        "risk_blocked": {
            "candidates": risk_candidates,
            "audit_records": risk_records,
        },
        "ready_for_human_review": ready_for_review,
    }


@router.get("/evaluation/{evaluation_id}")
async def get_evaluation_detail(
    evaluation_id: str,
    _user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """Full detail for one evaluation — including the three
    underlying LLM call ledger rows by `call_id` lookup."""
    rec = await db[PARADOX_RECORDS].find_one({"evaluation_id": evaluation_id})
    if not rec:
        from fastapi import HTTPException
        raise HTTPException(status_code=404,
                            detail=f"evaluation {evaluation_id!r} not found")
    rec = _strip(rec)

    # Pull the three ledger rows for the inline preview.
    call_ids_obj = rec.get("llm_call_ids") or {}
    call_ids = [cid for cid in (
        call_ids_obj.get("strategist"),
        call_ids_obj.get("opponent"),
        call_ids_obj.get("auditor"),
    ) if cid]
    ledger_rows: Dict[str, Dict[str, Any]] = {}
    if call_ids:
        async for d in db[LLM_CALLS].find(
            {"call_id": {"$in": call_ids}},
            {"_id": 0, "call_id": 1, "role": 1, "provider": 1,
             "model": 1, "latency_ms": 1, "ok": 1, "response": 1},
        ):
            cid = d.get("call_id")
            # truncate response preview
            resp = d.get("response") or ""
            d["response_preview"] = (
                resp if len(resp) <= 200 else f"{resp[:199]}…"
            )
            d.pop("response", None)
            ledger_rows[cid] = d
    return {"ok": True, "evaluation": rec, "ledger_rows": ledger_rows}
