"""CHEVELLE/HELLCAT crypto intent bridge.

Doctrine (2026-02-20):
    Bridges HELLCAT's decision output to MC's intent ledger. Mirrors
    `redeye_crypto_intent_bridge.py` exactly — same shape, same guard
    rails, same research-evidence attach. The only differences are
    the brain identifier (`hellcat`/`chevelle`) and the route prefix
    (`/api/admin/hellcat/bridge`).

    Two surfaces:
      * build_hellcat_crypto_intent(...)   — pure builder (returns dict)
      * emit_hellcat_crypto_intent(...)    — persists into shared_intents
      * POST /api/admin/hellcat/bridge/emit — operator/HELLCAT-sidecar entry

    Doctrine guards preserved:
      * crypto_only — bridge refuses non-crypto symbols
      * intent_only — `may_execute=False`, `requires_gate_pass=True`
      * hold_not_promotable — HOLD action is rejected
      * seat_based_final_authority — recipient = current crypto seat
      * research_is_evidence — Research Layer signals stamped on
        `evidence.research_signals` ONLY; brain decision fields are
        never overwritten.
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
    the crypto lane. Falls back to the literal string `crypto_executor`
    if the seat is vacant — audit marker, not routable."""
    for seat_name in seats_with_execute("crypto"):
        holder = await get_seat_holder(seat_name)
        if holder:
            return _norm(holder)
    return "crypto_executor"


# ──────────────────────── intent builder ────────────────────────

async def build_hellcat_crypto_intent(
    *,
    symbol: str,
    action: Literal["BUY", "SELL", "SHORT", "COVER"],
    confidence: float,
    thesis: str,
    source_doc: Optional[dict] = None,
    attach_research: bool = True,
) -> dict:
    """Compose a properly-shaped MC intent for HELLCAT on the crypto
    lane. Returns the dict — does NOT persist.

    Same contract as `build_redeye_crypto_intent`. See its docstring
    for full doctrine notes.
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
    intent = {
        "intent_id": f"chevelle-crypto-{action.lower()}-{uuid.uuid4().hex}",
        "stack": "hellcat",                   # MC's emitting-brain field
        "source": "hellcat",                  # snippet alias
        "lane": "crypto",
        "asset_class": "crypto",
        "symbol": sym,
        "action": action.upper(),
        "direction": action.upper(),             # snippet alias
        "confidence": float(confidence),
        "rationale": thesis,                     # MC's canonical field
        "thesis": thesis,                        # snippet alias
        "evidence": {
            "source_doc": source_doc or {},
            "bridge": "chevelle_crypto_intent_bridge",
        },
        # ── MC safety invariants (pinned) ──
        "may_execute": False,
        "requires_gate_pass": True,
        # ── Doctrine (mirrors redeye bridge) ──
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
        "ingest_method": "chevelle_crypto_bridge",
    }

    if attach_research:
        from shared.research.intent_evidence import attach_research_evidence
        await attach_research_evidence(intent)

    return intent


# ──────────────────────── crypto symbol predicate ────────────────────────

CRYPTO_BASES = frozenset({
    "BTC", "ETH", "SOL", "XRP", "ADA", "DOT", "AVAX", "LINK", "MATIC",
    "LTC", "BCH", "DOGE", "ATOM", "FIL", "ETC", "NEAR", "ALGO", "SAND",
    "MANA", "AAVE", "UNI", "COMP", "MKR", "SNX", "CRV", "SUSHI",
})


def _looks_like_crypto(symbol: str) -> bool:
    s = symbol.upper().strip()
    if "/" in s:
        return True
    if any(s.endswith(suf) for suf in ("USD", "USDT", "USDC", "EUR", "BTC", "ETH")):
        return True
    return s in CRYPTO_BASES


# ──────────────────────── persistence ────────────────────────

async def emit_hellcat_crypto_intent(
    *,
    symbol: str,
    action: Literal["BUY", "SELL", "SHORT", "COVER"],
    confidence: float,
    thesis: str,
    source_doc: Optional[dict] = None,
) -> dict:
    """Build, validate authority, and persist into shared_intents.

    Mirrors `emit_redeye_crypto_intent`. The seat-vacant and
    rotation-race fast-fail checks are identical.
    """
    intent = await build_hellcat_crypto_intent(
        symbol=symbol,
        action=action,
        confidence=confidence,
        thesis=thesis,
        source_doc=source_doc,
    )
    expected_authority = _norm(await _crypto_final_authority())
    actual_authority = _norm(intent.get("requires_final_authority"))
    if actual_authority != expected_authority:
        return {
            "allowed": False,
            "reason": "FINAL_AUTHORITY_SEAT_MISMATCH",
            "expected": expected_authority,
            "actual": actual_authority,
            "intent": intent,
        }
    if expected_authority == "crypto_executor":
        return {
            "allowed": False,
            "reason": "CRYPTO_SEAT_VACANT",
            "intent": intent,
        }
    await db[SHARED_INTENTS].insert_one(dict(intent))
    return {"allowed": True, "intent": intent}


# ──────────────────────── REST surface ────────────────────────

router = APIRouter(prefix="/admin/hellcat/bridge", tags=["hellcat_bridge"])


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
    """HELLCAT's crypto decision → MC intent. Same shape as the
    REDEYE bridge; the only operational difference is the emitting
    brain identifier."""
    return await emit_hellcat_crypto_intent(
        symbol=body.symbol,
        action=body.action,
        confidence=body.confidence,
        thesis=body.thesis,
        source_doc=body.source_doc,
    )


@router.get("/authority")
async def get_authority(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Returns the brain that any new HELLCAT crypto intent will be
    addressed to. Returns `'crypto_executor'` (marker) if the seat is
    vacant — bridge will refuse to emit until operator assigns."""
    holder = await _crypto_final_authority()
    return {
        "lane": "crypto",
        "final_authority": holder,
        "seat_vacant": holder == "crypto_executor",
        "authority_model": "seat_based",
    }
