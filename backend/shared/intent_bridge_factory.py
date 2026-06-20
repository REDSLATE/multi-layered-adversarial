"""Generic per-brain, per-lane intent bridge factory.

Eliminates the ~250-line copy/paste between
`redeye_crypto_intent_bridge.py` and `chevelle_crypto_intent_bridge.py`
and lets us spin up a new (brain, lane) bridge with a single call —
which we now need because every brain is supposed to be able to emit
in both lanes eventually.

Doctrine pinned by this module (NOT the per-brain wrapper):
    * Lane symbol predicate enforces `crypto_only` / `equity_only`.
    * `HOLD` action is rejected here, never per-brain.
    * `requires_final_authority` is stamped from `seats_with_execute(lane)`
      — seat-by-lane, never pair-by-symbol, never hardcoded brain.
    * `may_execute=False`, `requires_gate_pass=True` are pinned.
    * Research evidence attached via `shared.research.intent_evidence`
      (best-effort; bar errors get captured in `research_status`,
      never block emit).

The legacy crypto bridges are not refactored to use this factory yet
(they're working in production and the test surface around them is
deep) — those stay in place. New (brain, lane) bridges should be
built via `make_intent_bridge(...)` only.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db
from namespaces import SHARED_INTENTS
from shared.executor_seat import get_seat_holder, seats_with_execute


# ──────────────────────── lane symbol predicates ────────────────────────

CRYPTO_BASES = frozenset({
    "BTC", "ETH", "SOL", "XRP", "ADA", "DOT", "AVAX", "LINK", "MATIC",
    "LTC", "BCH", "DOGE", "ATOM", "FIL", "ETC", "NEAR", "ALGO", "SAND",
    "MANA", "AAVE", "UNI", "COMP", "MKR", "SNX", "CRV", "SUSHI",
})

_CRYPTO_SUFFIXES = ("USD", "USDT", "USDC", "EUR", "BTC", "ETH")


def looks_like_crypto(symbol: str) -> bool:
    s = symbol.upper().strip()
    if "/" in s:
        return True
    if any(s.endswith(suf) for suf in _CRYPTO_SUFFIXES):
        return True
    return s in CRYPTO_BASES


def looks_like_equity(symbol: str) -> bool:
    """Equity ticker shape: 1–6 alpha chars (allow `.` for class
    shares like BRK.B). Reject anything that looks crypto so we
    don't accidentally accept BTC as an "equity"."""
    s = symbol.upper().strip()
    if "/" in s or s in CRYPTO_BASES:
        return False
    if any(s.endswith(suf) for suf in _CRYPTO_SUFFIXES):
        return False
    if not (1 <= len(s) <= 6):
        return False
    return all(c.isalpha() or c == "." for c in s)


_LANE_PREDICATES: dict[str, Callable[[str], bool]] = {
    "crypto": looks_like_crypto,
    "equity": looks_like_equity,
}


# ──────────────────────── bridge config ────────────────────────

@dataclass(frozen=True)
class BridgeConfig:
    """Static config for one (brain, lane) bridge instance."""
    brain_id: str                # canonical: "camino"|"barracuda"|"hellcat"|"gto"
    lane: Literal["crypto", "equity"]
    runtime_alias: str           # "alpha"|"camaro"|"chevelle"|"redeye" — used in intent_id prefix
    roadguard_name: str          # "CryptoRoadGuard"|"EquityRoadGuard"
    route_prefix: str            # FastAPI prefix, e.g. /admin/camino/equity-bridge


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(x: Optional[str]) -> str:
    return str(x or "").lower().strip()


# Lifted to module scope so FastAPI can resolve it as a request body
# annotation. (Defined inside the factory closure, FastAPI mis-routes
# it as a query parameter named "body" and returns 422.)
class _EmitBody(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=24)
    action: Literal["BUY", "SELL", "SHORT", "COVER"]
    confidence: float = Field(..., ge=0.0, le=1.0)
    thesis: str = Field(..., min_length=1, max_length=4000)
    source_doc: Optional[dict] = None


# ──────────────────────── factory ────────────────────────

def make_intent_bridge(cfg: BridgeConfig):
    """Build a (build_fn, emit_fn, router) triple for the given config.

    Caller is responsible for registering `router` with the FastAPI
    app. Build/emit functions are returned so unit tests can call
    them directly without hitting the HTTP layer.
    """
    predicate = _LANE_PREDICATES.get(cfg.lane)
    if predicate is None:
        raise ValueError(f"unknown lane {cfg.lane!r}; must be one of {sorted(_LANE_PREDICATES)}")

    async def _final_authority() -> str:
        """Whoever holds an execute-capable seat for this lane RIGHT
        NOW. Falls back to the marker `<lane>_executor` if the seat is
        vacant — bridge will refuse to emit in that case."""
        for seat_name in seats_with_execute(cfg.lane):
            holder = await get_seat_holder(seat_name)
            if holder:
                return _norm(holder)
        return f"{cfg.lane}_executor"

    async def build(
        *,
        symbol: str,
        action: Literal["BUY", "SELL", "SHORT", "COVER"],
        confidence: float,
        thesis: str,
        source_doc: Optional[dict] = None,
        attach_research: bool = True,
    ) -> dict:
        sym = symbol.upper().strip()
        if action.upper() == "HOLD":
            raise HTTPException(
                status_code=400,
                detail="bridge refuses to promote HOLDs (doctrine: hold_not_promotable)",
            )
        if not predicate(sym):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"symbol {sym!r} does not look like {cfg.lane} "
                    f"(doctrine: {cfg.lane}_only)"
                ),
            )

        authority = await _final_authority()
        now = _now_iso()
        intent = {
            "intent_id": (
                f"{cfg.runtime_alias}-{cfg.lane}-"
                f"{action.lower()}-{uuid.uuid4().hex}"
            ),
            "stack": cfg.brain_id,
            "source": cfg.brain_id,
            "lane": cfg.lane,
            "asset_class": cfg.lane,
            "symbol": sym,
            "action": action.upper(),
            "direction": action.upper(),
            "confidence": float(confidence),
            "rationale": thesis,
            "thesis": thesis,
            "evidence": {
                "source_doc": source_doc or {},
                "bridge": f"{cfg.runtime_alias}_{cfg.lane}_intent_bridge",
            },
            # ── MC safety invariants (pinned) ──
            "may_execute": False,
            "requires_gate_pass": True,
            # ── Doctrine ──
            "requires_final_authority": authority,
            "authority_model": "seat_based",
            "requires_roadguard": True,
            "requires_guard": cfg.roadguard_name,
            "doctrine": {
                f"{cfg.lane}_only": True,
                "intent_only": True,
                "hold_not_promotable": True,
                "seat_based_final_authority": True,
                f"{cfg.lane}_roadguard_required": True,
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
            "ingest_method": f"{cfg.runtime_alias}_{cfg.lane}_bridge",
        }

        if attach_research:
            # Imported lazily so unit tests can patch the bar source
            # by name (`shared.research.intent_evidence.load_recent_bars`).
            from shared.research.intent_evidence import attach_research_evidence
            await attach_research_evidence(intent)

        return intent

    async def emit(
        *,
        symbol: str,
        action: Literal["BUY", "SELL", "SHORT", "COVER"],
        confidence: float,
        thesis: str,
        source_doc: Optional[dict] = None,
    ) -> dict:
        intent = await build(
            symbol=symbol,
            action=action,
            confidence=confidence,
            thesis=thesis,
            source_doc=source_doc,
        )
        expected = _norm(await _final_authority())
        actual = _norm(intent.get("requires_final_authority"))
        if actual != expected:
            return {
                "allowed": False,
                "reason": "FINAL_AUTHORITY_SEAT_MISMATCH",
                "expected": expected,
                "actual": actual,
                "intent": intent,
            }
        if expected == f"{cfg.lane}_executor":
            return {
                "allowed": False,
                "reason": f"{cfg.lane.upper()}_SEAT_VACANT",
                "intent": intent,
            }
        await db[SHARED_INTENTS].insert_one(dict(intent))
        return {"allowed": True, "intent": intent}

    # ────────── REST surface ──────────
    router = APIRouter(
        prefix=cfg.route_prefix,
        tags=[f"{cfg.brain_id}_{cfg.lane}_bridge"],
    )

    @router.post("/emit")
    async def emit_endpoint(
        body: _EmitBody,
        _user: dict = Depends(get_current_user),  # noqa: B008
    ):
        return await emit(
            symbol=body.symbol,
            action=body.action,
            confidence=body.confidence,
            thesis=body.thesis,
            source_doc=body.source_doc,
        )

    @router.get("/authority")
    async def get_authority_endpoint(
        _user: dict = Depends(get_current_user),  # noqa: B008
    ):
        holder = await _final_authority()
        return {
            "lane": cfg.lane,
            "brain": cfg.brain_id,
            "final_authority": holder,
            "seat_vacant": holder == f"{cfg.lane}_executor",
            "authority_model": "seat_based",
        }

    return build, emit, router
