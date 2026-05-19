"""HTTP surface for the platform survival layer.

These endpoints are ADDITIVE — they do not displace the existing
`/api/ingest/intent` or `/api/execution/*` paths. They expose the
portable survival layer (`shared/runtime/platform_survival.py`) over
HTTP so any brain sidecar (regardless of hosting platform) can:

  * POST its boot-time RuntimeStamp for validation
  * POST an intent envelope and receive a signed MCExecutionReceipt

Future work: wire `mc_canonical_gate(...)` into the auto-router so the
HMAC-signed receipt is the *only* thing the broker adapter trusts.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth import get_current_user
from shared.runtime.platform_survival import (
    RuntimeStamp,
    broker_verify_receipt,
    mc_canonical_gate,
    policy_hash,
)
from shared.calibration.snapshot_contract import contract_payload


router = APIRouter(prefix="/runtime/survival", tags=["platform-survival"])


# ─────────────────────── Schemas ──────────────────────────────────────


class StampValidateRequest(BaseModel):
    stamp: Dict[str, Any]


class IntentGateRequest(BaseModel):
    intent: Dict[str, Any]


class ReceiptVerifyRequest(BaseModel):
    receipt: Dict[str, Any]


# ─────────────────────── Endpoints ────────────────────────────────────


@router.get("/policy-hash")
async def get_policy_hash():
    """Sidecars hit this at boot to confirm they ship the same policy
    constitution as MC. If hashes diverge, the sidecar is stale and
    must redeploy. No auth: cheap, non-mutating, doctrine-pinned read.
    """
    return {
        "policy_hash": policy_hash(),
        "doctrine": (
            "sidecars communicate · MC approves · RoadGuard protects · "
            "broker executes only with MC receipt · preview is not PROD"
        ),
    }


@router.get("/snapshot-contract")
async def get_snapshot_contract():
    """Sidecars hit this at boot to fetch MC's canonical snapshot
    field contract (minimum + per-lane full sets). If the brain's
    local copy of the contract diverges from `contract_hash`, the
    brain is shipping snapshots MC will interpret with sentinel
    defaults — operator must redeploy the brain with the synced
    contract. No auth: doctrine-pinned read, same pattern as
    `/policy-hash`.
    """
    return contract_payload()


@router.post("/validate-stamp")
async def validate_stamp(
    body: StampValidateRequest,
    _user: dict = Depends(get_current_user),
):
    """Validate a sidecar's RuntimeStamp against the PROD doctrine.
    Returns a structured pass/fail with the list of errors so the
    operator dashboard can render the failure mode.
    """
    try:
        stamp = RuntimeStamp(**body.stamp)
    except TypeError as e:
        return {"ok": False, "errors": [f"STAMP_SHAPE_INVALID:{e}"], "stamp": body.stamp}
    return stamp.validate_for_prod_sidecar()


@router.post("/canonical-gate")
async def canonical_gate(
    body: IntentGateRequest,
    _user: dict = Depends(get_current_user),
):
    """Run the MC canonical gate on a sidecar-built intent envelope.
    Returns the signed MCExecutionReceipt. The broker adapter is the
    only party that verifies the signature; sidecars MUST treat the
    receipt as opaque.
    """
    return mc_canonical_gate(body.intent)


@router.post("/verify-receipt")
async def verify_receipt(
    body: ReceiptVerifyRequest,
    _user: dict = Depends(get_current_user),
):
    """Broker adapters call this just before placing an order. Returns
    `{ok, reason, lane, symbol, direction}` — refuse the order on any
    `ok=False`.
    """
    return broker_verify_receipt(body.receipt)
