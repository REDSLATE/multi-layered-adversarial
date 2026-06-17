"""REDEYE crypto intent bridge.

Doctrine (2026-02-16):
    Bridges REDEYE's decision output to MC's intent ledger. The
    intent's final-authority recipient is whoever currently holds the
    crypto execute seat — NOT a hardcoded brain name. This keeps the
    bridge correct across roster rotations.

    The snippet originally proposed `get_executor_holder(lane="crypto")`
    but MC's lane-aware seat helpers are `seats_with_execute(lane)` +
    `get_seat_holder(seat)`. We use the real API.

    Two surfaces:
      * build_redeye_crypto_intent(...)   — pure builder (returns dict)
      * emit_redeye_crypto_intent(...)    — persists into shared_intents
      * POST /api/admin/redeye/bridge/emit — operator/REDEYE-sidecar entry

    Doctrine guards preserved from the snippet:
      * crypto_only — bridge refuses non-crypto symbols
      * intent_only — `may_execute=False`, `requires_gate_pass=True`
      * hold_not_promotable — HOLD action is rejected
      * seat_based_final_authority — recipient = current crypto seat
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import SHARED_INTENTS
from shared.executor_seat import get_seat_holder, seats_with_execute


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(x: Optional[str]) -> str:
    return str(x or "").lower().strip()


async def _crypto_final_authority() -> str:
    """Return the brain currently holding an execute-capable seat in
    the crypto lane. Falls back to the literal string "crypto_executor"
    if the seat is vacant — that's an audit marker, not a routable
    target. (Mirrors the snippet's contract.)"""
    for seat_name in seats_with_execute("crypto"):
        holder = await get_seat_holder(seat_name)
        if holder:
            return _norm(holder)
    return "crypto_executor"


# ──────────────────────── intent builder ────────────────────────

async def build_redeye_crypto_intent(
    *,
    symbol: str,
    action: Literal["BUY", "SELL", "SHORT", "COVER"],
    confidence: float,
    thesis: str,
    source_doc: Optional[dict] = None,
) -> dict:
    """Compose a properly-shaped MC intent for REDEYE on the crypto
    lane. Returns the dict — does NOT persist.

    Doctrine guards:
      * Symbol must look crypto (contains '/' OR ends with USD/USDT/USDC
        OR matches a known crypto base like BTC/ETH/SOL/etc.).
      * action='HOLD' is forbidden — bridge refuses to promote HOLDs.
      * `may_execute=False` and `requires_gate_pass=True` are pinned;
        the gate chain still owns the execute decision.
    """
    sym = symbol.upper().strip()
    if action.upper() == "HOLD":
        raise HTTPException(
            status_code=400,
            detail="bridge refuses to promote HOLDs (doctrine: hold_not_promotable)",
        )
    if not _looks_like_crypto(sym):
        raise HTTPException(
            status_code=400,
            detail=f"symbol {sym!r} does not look like crypto (doctrine: crypto_only)",
        )

    final_authority = await _crypto_final_authority()
    now = _now_iso()
    return {
        "intent_id": f"redeye-crypto-{action.lower()}-{uuid.uuid4().hex}",
        "stack": "gto",                  # MC's emitting-brain field
        "source": "gto",                 # snippet alias
        "lane": "crypto",
        "asset_class": "crypto",
        "symbol": sym,
        "action": action.upper(),
        "direction": action.upper(),        # snippet alias
        "confidence": float(confidence),
        "rationale": thesis,                # MC's canonical field
        "thesis": thesis,                   # snippet alias
        "evidence": {
            "source_doc": source_doc or {},
            "bridge": "redeye_crypto_intent_bridge",
        },
        # ── MC safety invariants (pinned) ──
        "may_execute": False,
        "requires_gate_pass": True,
        # ── Doctrine (mirrors snippet) ──
        "requires_final_authority": final_authority,
        "authority_model": "seat_based",
        "requires_roadguard": True,
        "requires_guard": "CryptoRoadGuard",
        "doctrine": {
            "crypto_only": True,
            "intent_only": True,
            "hold_not_promotable": True,
            "seat_based_final_authority": True,
            "crypto_roadguard_required": True,
        },
        # ── Lifecycle ──
        "status": "PENDING",
        "gate_state": "pending",
        "executed": False,
        "executed_at": None,
        "execution_receipt_id": None,
        # ── Audit ──
        "created_at": now,
        "updated_at": now,
        "ingest_ts": now,
        "ingest_method": "redeye_crypto_bridge",
    }


CRYPTO_BASES = {
    "BTC", "ETH", "SOL", "ADA", "DOGE", "ATOM", "XRP", "DOT", "AVAX",
    "MATIC", "LINK", "LTC", "BCH", "UNI", "FIL", "TRX", "ALGO", "XLM",
    "NEAR", "APT", "ARB", "OP", "INJ", "SUI", "TIA", "RUNE",
}


def _looks_like_crypto(symbol: str) -> bool:
    s = symbol.upper().strip()
    if "/" in s:
        return True
    if any(s.endswith(suf) for suf in ("USD", "USDT", "USDC", "EUR", "BTC", "ETH")):
        return True
    return s in CRYPTO_BASES


# ──────────────────────── persistence ────────────────────────

async def emit_redeye_crypto_intent(
    *,
    symbol: str,
    action: Literal["BUY", "SELL", "SHORT", "COVER"],
    confidence: float,
    thesis: str,
    source_doc: Optional[dict] = None,
) -> dict:
    """Build, validate authority, and persist into shared_intents.

    Validates that the intent's `requires_final_authority` matches the
    crypto seat holder at ingest time. If the seat is vacant or the
    authority lookup somehow disagrees with itself between build and
    insert (rotation race), returns a structured error and skips the
    insert. The gate chain will re-validate seat-holding again at
    execution time — this is a fast-fail check only.
    """
    intent = await build_redeye_crypto_intent(
        symbol=symbol,
        action=action,
        confidence=confidence,
        thesis=thesis,
        source_doc=source_doc,
    )
    expected_authority = _norm(await _crypto_final_authority())
    actual_authority = _norm(intent.get("requires_final_authority"))
    if actual_authority != expected_authority:
        # Should never trigger unless the roster rotates inside this
        # function — surface as a structured rejection.
        return {
            "allowed": False,
            "reason": "FINAL_AUTHORITY_SEAT_MISMATCH",
            "expected": expected_authority,
            "actual": actual_authority,
            "intent": intent,
        }
    if expected_authority == "crypto_executor":
        # Marker value from _crypto_final_authority — seat is vacant.
        return {
            "allowed": False,
            "reason": "CRYPTO_SEAT_VACANT",
            "intent": intent,
        }
    await db[SHARED_INTENTS].insert_one(dict(intent))
    return {"allowed": True, "intent": intent}


# ──────────────────────── REST surface ────────────────────────

router = APIRouter(prefix="/admin/redeye/bridge", tags=["redeye_bridge"])


class EmitBody(BaseModel):
    symbol: str = Field(..., min_length=2, max_length=24)
    action: Literal["BUY", "SELL", "SHORT", "COVER"]
    confidence: float = Field(..., ge=0.0, le=1.0)
    thesis: str = Field(..., min_length=1, max_length=4000)
    source_doc: Optional[dict] = None


@router.post("/emit")
async def emit_endpoint(
    body: EmitBody,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """REDEYE's crypto decision → MC intent. Pasted intent dict has the
    crypto seat holder stamped as `requires_final_authority`. Refuses
    if seat is vacant (CRYPTO_SEAT_VACANT) or non-crypto symbol."""
    return await emit_redeye_crypto_intent(
        symbol=body.symbol,
        action=body.action,
        confidence=body.confidence,
        thesis=body.thesis,
        source_doc=body.source_doc,
    )


@router.get("/authority")
async def get_authority(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Returns the brain that any new REDEYE crypto intent will be
    addressed to. Returns `'crypto_executor'` (marker) if the seat is
    vacant — bridge will refuse to emit until operator assigns."""
    holder = await _crypto_final_authority()
    return {
        "lane": "crypto",
        "final_authority": holder,
        "seat_vacant": holder == "crypto_executor",
        "authority_model": "seat_based",
    }
