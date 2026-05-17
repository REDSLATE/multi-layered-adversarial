"""REST surface for deterministic risk guards.

Per-lane endpoints so equity and crypto take-profit are distinct paths
even though they share the math:

  POST  /api/admin/risk/take-profit/evaluate
        Pure math — accepts side/entry/current/tp_pct, returns the
        verdict. Stateless, no DB read, lane-agnostic.

  POST  /api/admin/risk/equity/take-profit/check/{position_id}
        Look up an equity live position, evaluate. Does not act.

  POST  /api/admin/risk/equity/take-profit/enforce/{position_id}
        Evaluate AND act. Calls live_positions.close or
        record_management as the verdict dictates. Brain advisory
        cannot override.

  POST  /api/admin/risk/crypto/take-profit/check/{position_id}
        Same as equity but lane='crypto'.

  POST  /api/admin/risk/crypto/take-profit/enforce/{position_id}
        Same.

Doctrine pinned by the route prefix — there is NO union endpoint that
silently picks the lane. The caller must address the right lane.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from auth import get_current_user
from shared.crypto.take_profit import (
    enforce_position as crypto_enforce_position,
    evaluate_position as crypto_evaluate_position,
)
from shared.equity.take_profit import (
    enforce_position as equity_enforce_position,
    evaluate_position as equity_evaluate_position,
)
from shared.risk.take_profit_guard import take_profit_guard


router = APIRouter(prefix="/admin/risk", tags=["risk"])


# ───────────────────── pure-math endpoint (lane-agnostic) ─────────────────────

class EvaluateBody(BaseModel):
    side: Literal["LONG", "SHORT"]
    entry_price: float = Field(..., gt=0)
    current_price: float = Field(..., gt=0)
    take_profit_pct: float = Field(default=3.0, gt=0)
    partial_take_pct: Optional[float] = Field(default=None, gt=0)
    partial_close_fraction: float = Field(default=0.50, gt=0, le=1.0)


@router.post("/take-profit/evaluate")
async def evaluate_endpoint(
    body: EvaluateBody,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Pure deterministic math. No DB read, no side effect."""
    v = take_profit_guard(
        side=body.side,
        entry_price=body.entry_price,
        current_price=body.current_price,
        take_profit_pct=body.take_profit_pct,
        partial_take_pct=body.partial_take_pct,
        partial_close_fraction=body.partial_close_fraction,
    )
    return {
        "action": v.action,
        "reason": v.reason,
        "pnl_pct": v.pnl_pct,
        "target_pct": v.target_pct,
        "close_fraction": v.close_fraction,
    }


# ───────────────────── lane-scoped position endpoints ─────────────────────

class PositionGuardBody(BaseModel):
    current_price: float = Field(..., gt=0)
    take_profit_pct: float = Field(default=3.0, gt=0)
    partial_take_pct: Optional[float] = Field(default=None, gt=0)
    partial_close_fraction: float = Field(default=0.50, gt=0, le=1.0)


# ── EQUITY (Camaro's executor lane) ──

@router.post("/equity/take-profit/check/{position_id}")
async def equity_check(
    position_id: str,
    body: PositionGuardBody,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    return await equity_evaluate_position(
        position_id=position_id,
        current_price=body.current_price,
        take_profit_pct=body.take_profit_pct,
        partial_take_pct=body.partial_take_pct,
        partial_close_fraction=body.partial_close_fraction,
    )


@router.post("/equity/take-profit/enforce/{position_id}")
async def equity_enforce(
    position_id: str,
    body: PositionGuardBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    return await equity_enforce_position(
        position_id=position_id,
        current_price=body.current_price,
        actor=(user.get("email") or "operator") + " · take_profit_guard",
        take_profit_pct=body.take_profit_pct,
        partial_take_pct=body.partial_take_pct,
        partial_close_fraction=body.partial_close_fraction,
    )


# ── CRYPTO (REDEYE's executor lane) ──

@router.post("/crypto/take-profit/check/{position_id}")
async def crypto_check(
    position_id: str,
    body: PositionGuardBody,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    return await crypto_evaluate_position(
        position_id=position_id,
        current_price=body.current_price,
        take_profit_pct=body.take_profit_pct,
        partial_take_pct=body.partial_take_pct,
        partial_close_fraction=body.partial_close_fraction,
    )


@router.post("/crypto/take-profit/enforce/{position_id}")
async def crypto_enforce(
    position_id: str,
    body: PositionGuardBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    return await crypto_enforce_position(
        position_id=position_id,
        current_price=body.current_price,
        actor=(user.get("email") or "operator") + " · take_profit_guard",
        take_profit_pct=body.take_profit_pct,
        partial_take_pct=body.partial_take_pct,
        partial_close_fraction=body.partial_close_fraction,
    )
