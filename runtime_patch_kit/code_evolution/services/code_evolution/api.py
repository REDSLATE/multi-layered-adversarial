"""HTTP API for Code Evolution v0.

Endpoints (all require an authenticated operator):

    POST /api/admin/code-evolution/audit                 → run AST + classifier, persist proposal
    GET  /api/admin/code-evolution/proposals             → list proposals
    GET  /api/admin/code-evolution/proposals/{id}        → fetch a proposal + signoffs
    POST /api/admin/code-evolution/{id}/countersign      → operator countersign
    POST /api/admin/code-evolution/{id}/reject           → operator reject

Doctrine mirrored at every endpoint:
    - PROTECTED → returns HTTP 423 Locked. No countersign endpoint can override.
    - CRITICAL  → required_signatures=2 (dual-sign, mirror Build 3)
    - The same operator cannot sign a proposal twice (HTTP 409).
    - may_auto_promote() returns False — there is no "approve" endpoint.

The router does not import any stack-specific module. All stack-specific
behaviour (auth, storage) flows through `deps.get_current_user` and
`deps.get_dispatcher`. Each stack replaces deps.py.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from . import deps
from .ast_invariants import scan_invariants
from .code_auditor import classify
from .promotion_policy import (
    cool_down_seconds_for,
    may_auto_promote,
    required_signatures_for,
)
from .receipts import ReceiptDispatcher
from .schemas import (
    AuditRequest,
    AuditResponse,
    CountersignBody,
    now_iso,
)


router = APIRouter(prefix="/admin/code-evolution", tags=["code-evolution"])


# ─────────────────────────── /audit ───────────────────────────

@router.post("/audit", response_model=AuditResponse)
async def audit_patch(
    body: AuditRequest,
    user: dict[str, Any] = Depends(deps.get_current_user),
    dispatcher: ReceiptDispatcher = Depends(deps.get_dispatcher),
) -> AuditResponse:
    proposal_id = str(uuid.uuid4())

    invariant = scan_invariants(
        proposal_id=proposal_id,
        target_files=body.target_files,
        post_patch_files=body.post_patch_files,
    )
    audit = classify(invariant)

    # PROTECTED → BLOCKED. Persist a record so the attempt is auditable, then
    # return 423 so the caller (and any AI strategist) gets a hard stop.
    if audit.classification == "PROTECTED":
        await dispatcher.upsert_proposal({
            "proposal_id": proposal_id,
            "title": body.title,
            "rationale": body.rationale,
            "target_files": body.target_files,
            "diff_text": body.diff_text,
            "post_patch_files": body.post_patch_files,
            "proposed_by": user.get("email"),
            "created_at": now_iso(),
            "status": "BLOCKED",
            "invariant": invariant.__dict__,
            "audit": audit.__dict__,
            "signers": [],
            "signoffs": [],
        })
        raise HTTPException(
            status_code=423,
            detail={
                "proposal_id": proposal_id,
                "classification": "PROTECTED",
                "reason": "Patch touches Code Evolution gate. Out-of-band edit only.",
                "notes": audit.notes,
            },
        )

    # Invariant failure → record as INVARIANT_FAILED, classification still
    # surfaced so operator sees what would have been required.
    if not invariant.passed:
        status = "INVARIANT_FAILED"
    else:
        # Single-sign and dual-sign both start in AWAITING_SIGNATURE. The
        # countersign endpoint promotes to AWAITING_SECOND_SIGNATURE if the
        # classification requires more signatures than collected so far.
        status = "AWAITING_SIGNATURE"

    await dispatcher.upsert_proposal({
        "proposal_id": proposal_id,
        "title": body.title,
        "rationale": body.rationale,
        "target_files": body.target_files,
        "diff_text": body.diff_text,
        "post_patch_files": body.post_patch_files,
        "proposed_by": user.get("email"),
        "created_at": now_iso(),
        "status": status,
        "classification": audit.classification,
        "required_signatures": audit.required_signatures,
        "cool_down_seconds": audit.cool_down_seconds,
        "required_tests": audit.required_tests,
        "invariant": invariant.__dict__,
        "audit": audit.__dict__,
        "signers": [],
        "signoffs": [],
    })

    return AuditResponse(
        proposal_id=proposal_id,
        status=status,
        classification=audit.classification,
        required_signatures=audit.required_signatures,
        cool_down_seconds=audit.cool_down_seconds,
        required_tests=audit.required_tests,
        invariant=invariant.__dict__,
        notes=audit.notes,
    )


# ─────────────────────────── /proposals (list + get) ───────────────────────────

@router.get("/proposals")
async def list_proposals(
    status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    _user: dict[str, Any] = Depends(deps.get_current_user),
    dispatcher: ReceiptDispatcher = Depends(deps.get_dispatcher),
):
    items = await dispatcher.list_proposals(status=status, limit=limit)
    return {"items": items, "count": len(items)}


@router.get("/proposals/{proposal_id}")
async def get_proposal(
    proposal_id: str,
    _user: dict[str, Any] = Depends(deps.get_current_user),
    dispatcher: ReceiptDispatcher = Depends(deps.get_dispatcher),
):
    doc = await dispatcher.get_proposal(proposal_id)
    if not doc:
        raise HTTPException(status_code=404, detail="proposal not found")
    return doc


# ─────────────────────────── /countersign ───────────────────────────

@router.post("/{proposal_id}/countersign")
async def countersign(
    proposal_id: str,
    body: CountersignBody,
    user: dict[str, Any] = Depends(deps.get_current_user),
    dispatcher: ReceiptDispatcher = Depends(deps.get_dispatcher),
):
    # Doctrine line — referenced at the endpoint that operators invoke most.
    # The hard False below is what makes Code Evolution an audit layer rather
    # than an authority layer. Don't refactor it away.
    assert may_auto_promote(proposal_id=proposal_id) is False  # noqa: S101

    doc = await dispatcher.get_proposal(proposal_id)
    if not doc:
        raise HTTPException(status_code=404, detail="proposal not found")

    status = doc.get("status")
    if status not in ("AWAITING_SIGNATURE", "AWAITING_SECOND_SIGNATURE"):
        raise HTTPException(status_code=409, detail=f"proposal already {status}")

    classification = doc.get("classification", "LOW")
    required = required_signatures_for(classification)
    if required < 0:
        raise HTTPException(status_code=423, detail="proposal is BLOCKED; cannot countersign")

    operator_email = (user.get("email") or "").lower()
    signers = list(doc.get("signers") or [])
    if any((s.get("operator") or "").lower() == operator_email for s in signers):
        raise HTTPException(
            status_code=409,
            detail="this operator has already countersigned; a second, distinct operator is required",
        )

    new_signer = {"operator": user.get("email"), "at": now_iso(), "note": body.note}
    signers.append(new_signer)
    await dispatcher.append_signoff(proposal_id, {"event": "countersign", **new_signer})

    if len(signers) < required:
        await dispatcher.update_status(proposal_id, "AWAITING_SECOND_SIGNATURE", signers=signers)
        return {
            "ok": True,
            "awaiting_more_signatures": True,
            "signed": len(signers),
            "required": required,
            "classification": classification,
        }

    # Required signatures collected. v0 does NOT apply the patch — operator
    # applies it via their existing apply path (git, supervisor reload, etc.).
    # APPROVED here means "operator may now apply"; it does not write code.
    cool_down = cool_down_seconds_for(classification)
    await dispatcher.update_status(proposal_id, "APPROVED", signers=signers)
    return {
        "ok": True,
        "status": "APPROVED",
        "signed": len(signers),
        "required": required,
        "classification": classification,
        "cool_down_seconds": cool_down,
        "next_step": (
            "Operator may apply the patch out-of-band. Code Evolution "
            "does not write to disk and does not auto-promote."
        ),
    }


# ─────────────────────────── /reject ───────────────────────────

@router.post("/{proposal_id}/reject")
async def reject(
    proposal_id: str,
    body: CountersignBody,
    user: dict[str, Any] = Depends(deps.get_current_user),
    dispatcher: ReceiptDispatcher = Depends(deps.get_dispatcher),
):
    doc = await dispatcher.get_proposal(proposal_id)
    if not doc:
        raise HTTPException(status_code=404, detail="proposal not found")
    status = doc.get("status")
    if status not in ("AWAITING_SIGNATURE", "AWAITING_SECOND_SIGNATURE", "INVARIANT_FAILED"):
        raise HTTPException(status_code=409, detail=f"proposal already {status}")

    event = {"event": "reject", "operator": user.get("email"), "at": now_iso(), "note": body.note}
    await dispatcher.append_signoff(proposal_id, event)
    await dispatcher.update_status(proposal_id, "REJECTED")
    return {"ok": True, "status": "REJECTED"}
