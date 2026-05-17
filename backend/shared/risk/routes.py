"""REST surface for deterministic risk guards.

Per-lane endpoints so equity and crypto take-profit / stop-loss /
trailing-stop / max-hold-time are distinct paths even though they
share the math:

  Pure math (lane-agnostic):
    POST  /api/admin/risk/take-profit/evaluate
    POST  /api/admin/risk/stop-loss/evaluate
    POST  /api/admin/risk/trailing-stop/evaluate
    POST  /api/admin/risk/max-hold-time/evaluate

  Lane-scoped, look up + check (no side effect):
    POST  /api/admin/risk/equity/{guard}/check/{position_id}
    POST  /api/admin/risk/crypto/{guard}/check/{position_id}

  Lane-scoped, evaluate + act (close/reduce, broadcast outcome):
    POST  /api/admin/risk/equity/{guard}/enforce/{position_id}
    POST  /api/admin/risk/crypto/{guard}/enforce/{position_id}

  Position Monitor scheduler:
    GET   /api/admin/risk/monitor/status
    POST  /api/admin/risk/monitor/run-once    (one-shot, manual trigger)

Doctrine pinned by the route prefix — there is NO union endpoint that
silently picks the lane. The caller must address the right lane.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from auth import get_current_user
from shared.crypto.max_hold_time import (
    enforce_position as crypto_mht_enforce,
    evaluate_position as crypto_mht_evaluate,
)
from shared.crypto.stop_loss import (
    enforce_position as crypto_sl_enforce,
    evaluate_position as crypto_sl_evaluate,
)
from shared.crypto.take_profit import (
    enforce_position as crypto_tp_enforce,
    evaluate_position as crypto_tp_evaluate,
)
from shared.crypto.trailing_stop import (
    enforce_position as crypto_ts_enforce,
    evaluate_position as crypto_ts_evaluate,
)
from shared.equity.max_hold_time import (
    enforce_position as equity_mht_enforce,
    evaluate_position as equity_mht_evaluate,
)
from shared.equity.stop_loss import (
    enforce_position as equity_sl_enforce,
    evaluate_position as equity_sl_evaluate,
)
from shared.equity.take_profit import (
    enforce_position as equity_tp_enforce,
    evaluate_position as equity_tp_evaluate,
)
from shared.equity.trailing_stop import (
    enforce_position as equity_ts_enforce,
    evaluate_position as equity_ts_evaluate,
)
from shared.risk.max_hold_time_guard import max_hold_time_guard
from shared.risk.stop_loss_guard import stop_loss_guard
from shared.risk.take_profit_guard import take_profit_guard
from shared.risk.trailing_stop_guard import trailing_stop_guard


router = APIRouter(prefix="/admin/risk", tags=["risk"])


# ───────────────────── pure-math endpoints (lane-agnostic) ─────────────────────

class TakeProfitEvalBody(BaseModel):
    side: Literal["LONG", "SHORT"]
    entry_price: float = Field(..., gt=0)
    current_price: float = Field(..., gt=0)
    take_profit_pct: float = Field(default=3.0, gt=0)
    partial_take_pct: Optional[float] = Field(default=None, gt=0)
    partial_close_fraction: float = Field(default=0.50, gt=0, le=1.0)


@router.post("/take-profit/evaluate")
async def evaluate_take_profit(
    body: TakeProfitEvalBody,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    v = take_profit_guard(
        side=body.side,
        entry_price=body.entry_price,
        current_price=body.current_price,
        take_profit_pct=body.take_profit_pct,
        partial_take_pct=body.partial_take_pct,
        partial_close_fraction=body.partial_close_fraction,
    )
    return {
        "guard": "take_profit",
        "action": v.action, "reason": v.reason,
        "pnl_pct": v.pnl_pct, "target_pct": v.target_pct,
        "close_fraction": v.close_fraction,
    }


class StopLossEvalBody(BaseModel):
    side: Literal["LONG", "SHORT"]
    entry_price: float = Field(..., gt=0)
    current_price: float = Field(..., gt=0)
    stop_loss_pct: float = Field(default=2.0, gt=0)


@router.post("/stop-loss/evaluate")
async def evaluate_stop_loss(
    body: StopLossEvalBody,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    v = stop_loss_guard(
        side=body.side,
        entry_price=body.entry_price,
        current_price=body.current_price,
        stop_loss_pct=body.stop_loss_pct,
    )
    return {
        "guard": "stop_loss",
        "action": v.action, "reason": v.reason,
        "pnl_pct": v.pnl_pct, "target_pct": v.target_pct,
        "close_fraction": v.close_fraction,
    }


class TrailingStopEvalBody(BaseModel):
    side: Literal["LONG", "SHORT"]
    entry_price: float = Field(..., gt=0)
    current_price: float = Field(..., gt=0)
    previous_peak: Optional[float] = Field(default=None, gt=0)
    trail_pct: float = Field(default=1.5, gt=0)
    activate_after_pct: float = Field(default=1.0, gt=0)


@router.post("/trailing-stop/evaluate")
async def evaluate_trailing_stop(
    body: TrailingStopEvalBody,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    v = trailing_stop_guard(
        side=body.side,
        entry_price=body.entry_price,
        current_price=body.current_price,
        previous_peak=body.previous_peak,
        trail_pct=body.trail_pct,
        activate_after_pct=body.activate_after_pct,
    )
    return {
        "guard": "trailing_stop",
        "action": v.action, "reason": v.reason,
        "new_peak": v.new_peak,
        "pnl_from_peak_pct": v.pnl_from_peak_pct,
        "target_pct": v.target_pct,
        "close_fraction": v.close_fraction,
    }


class MaxHoldTimeEvalBody(BaseModel):
    opened_at: str = Field(..., min_length=1)
    max_hold_minutes: float = Field(default=60.0 * 24.0, gt=0)


@router.post("/max-hold-time/evaluate")
async def evaluate_max_hold_time(
    body: MaxHoldTimeEvalBody,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    v = max_hold_time_guard(
        opened_at=body.opened_at,
        max_hold_minutes=body.max_hold_minutes,
    )
    return {
        "guard": "max_hold_time",
        "action": v.action, "reason": v.reason,
        "held_for_minutes": v.held_for_minutes,
        "target_minutes": v.target_minutes,
        "close_fraction": v.close_fraction,
    }


# ───────────────────── lane-scoped position endpoints ─────────────────────

class TakeProfitPositionBody(BaseModel):
    current_price: float = Field(..., gt=0)
    take_profit_pct: float = Field(default=3.0, gt=0)
    partial_take_pct: Optional[float] = Field(default=None, gt=0)
    partial_close_fraction: float = Field(default=0.50, gt=0, le=1.0)


class StopLossPositionBody(BaseModel):
    current_price: float = Field(..., gt=0)
    stop_loss_pct: float = Field(default=2.0, gt=0)


class TrailingStopPositionBody(BaseModel):
    current_price: float = Field(..., gt=0)
    trail_pct: float = Field(default=1.5, gt=0)
    activate_after_pct: float = Field(default=1.0, gt=0)


class MaxHoldTimePositionBody(BaseModel):
    current_price: Optional[float] = Field(default=None, gt=0)
    max_hold_minutes: float = Field(default=60.0 * 24.0, gt=0)


# ── EQUITY ──

@router.post("/equity/take-profit/check/{position_id}")
async def equity_tp_check(position_id: str, body: TakeProfitPositionBody, _u: dict = Depends(get_current_user)):  # noqa: B008
    return await equity_tp_evaluate(
        position_id=position_id, current_price=body.current_price,
        take_profit_pct=body.take_profit_pct, partial_take_pct=body.partial_take_pct,
        partial_close_fraction=body.partial_close_fraction,
    )


@router.post("/equity/take-profit/enforce/{position_id}")
async def equity_tp_enforce_ep(position_id: str, body: TakeProfitPositionBody, user: dict = Depends(get_current_user)):  # noqa: B008
    return await equity_tp_enforce(
        position_id=position_id, current_price=body.current_price,
        actor=(user.get("email") or "operator") + " · take_profit_guard",
        take_profit_pct=body.take_profit_pct, partial_take_pct=body.partial_take_pct,
        partial_close_fraction=body.partial_close_fraction,
    )


@router.post("/equity/stop-loss/check/{position_id}")
async def equity_sl_check(position_id: str, body: StopLossPositionBody, _u: dict = Depends(get_current_user)):  # noqa: B008
    return await equity_sl_evaluate(position_id=position_id, current_price=body.current_price, stop_loss_pct=body.stop_loss_pct)


@router.post("/equity/stop-loss/enforce/{position_id}")
async def equity_sl_enforce_ep(position_id: str, body: StopLossPositionBody, user: dict = Depends(get_current_user)):  # noqa: B008
    return await equity_sl_enforce(
        position_id=position_id, current_price=body.current_price,
        actor=(user.get("email") or "operator") + " · stop_loss_guard",
        stop_loss_pct=body.stop_loss_pct,
    )


@router.post("/equity/trailing-stop/check/{position_id}")
async def equity_ts_check(position_id: str, body: TrailingStopPositionBody, _u: dict = Depends(get_current_user)):  # noqa: B008
    return await equity_ts_evaluate(
        position_id=position_id, current_price=body.current_price,
        trail_pct=body.trail_pct, activate_after_pct=body.activate_after_pct,
        persist_peak=False,  # check is non-mutating
    )


@router.post("/equity/trailing-stop/enforce/{position_id}")
async def equity_ts_enforce_ep(position_id: str, body: TrailingStopPositionBody, user: dict = Depends(get_current_user)):  # noqa: B008
    return await equity_ts_enforce(
        position_id=position_id, current_price=body.current_price,
        actor=(user.get("email") or "operator") + " · trailing_stop_guard",
        trail_pct=body.trail_pct, activate_after_pct=body.activate_after_pct,
    )


@router.post("/equity/max-hold-time/check/{position_id}")
async def equity_mht_check(position_id: str, body: MaxHoldTimePositionBody, _u: dict = Depends(get_current_user)):  # noqa: B008
    return await equity_mht_evaluate(
        position_id=position_id, current_price=body.current_price,
        max_hold_minutes=body.max_hold_minutes,
    )


@router.post("/equity/max-hold-time/enforce/{position_id}")
async def equity_mht_enforce_ep(position_id: str, body: MaxHoldTimePositionBody, user: dict = Depends(get_current_user)):  # noqa: B008
    return await equity_mht_enforce(
        position_id=position_id, current_price=body.current_price,
        actor=(user.get("email") or "operator") + " · max_hold_time_guard",
        max_hold_minutes=body.max_hold_minutes,
    )


# ── CRYPTO ──

@router.post("/crypto/take-profit/check/{position_id}")
async def crypto_tp_check(position_id: str, body: TakeProfitPositionBody, _u: dict = Depends(get_current_user)):  # noqa: B008
    return await crypto_tp_evaluate(
        position_id=position_id, current_price=body.current_price,
        take_profit_pct=body.take_profit_pct, partial_take_pct=body.partial_take_pct,
        partial_close_fraction=body.partial_close_fraction,
    )


@router.post("/crypto/take-profit/enforce/{position_id}")
async def crypto_tp_enforce_ep(position_id: str, body: TakeProfitPositionBody, user: dict = Depends(get_current_user)):  # noqa: B008
    return await crypto_tp_enforce(
        position_id=position_id, current_price=body.current_price,
        actor=(user.get("email") or "operator") + " · take_profit_guard",
        take_profit_pct=body.take_profit_pct, partial_take_pct=body.partial_take_pct,
        partial_close_fraction=body.partial_close_fraction,
    )


@router.post("/crypto/stop-loss/check/{position_id}")
async def crypto_sl_check(position_id: str, body: StopLossPositionBody, _u: dict = Depends(get_current_user)):  # noqa: B008
    return await crypto_sl_evaluate(position_id=position_id, current_price=body.current_price, stop_loss_pct=body.stop_loss_pct)


@router.post("/crypto/stop-loss/enforce/{position_id}")
async def crypto_sl_enforce_ep(position_id: str, body: StopLossPositionBody, user: dict = Depends(get_current_user)):  # noqa: B008
    return await crypto_sl_enforce(
        position_id=position_id, current_price=body.current_price,
        actor=(user.get("email") or "operator") + " · stop_loss_guard",
        stop_loss_pct=body.stop_loss_pct,
    )


@router.post("/crypto/trailing-stop/check/{position_id}")
async def crypto_ts_check(position_id: str, body: TrailingStopPositionBody, _u: dict = Depends(get_current_user)):  # noqa: B008
    return await crypto_ts_evaluate(
        position_id=position_id, current_price=body.current_price,
        trail_pct=body.trail_pct, activate_after_pct=body.activate_after_pct,
        persist_peak=False,
    )


@router.post("/crypto/trailing-stop/enforce/{position_id}")
async def crypto_ts_enforce_ep(position_id: str, body: TrailingStopPositionBody, user: dict = Depends(get_current_user)):  # noqa: B008
    return await crypto_ts_enforce(
        position_id=position_id, current_price=body.current_price,
        actor=(user.get("email") or "operator") + " · trailing_stop_guard",
        trail_pct=body.trail_pct, activate_after_pct=body.activate_after_pct,
    )


@router.post("/crypto/max-hold-time/check/{position_id}")
async def crypto_mht_check(position_id: str, body: MaxHoldTimePositionBody, _u: dict = Depends(get_current_user)):  # noqa: B008
    return await crypto_mht_evaluate(
        position_id=position_id, current_price=body.current_price,
        max_hold_minutes=body.max_hold_minutes,
    )


@router.post("/crypto/max-hold-time/enforce/{position_id}")
async def crypto_mht_enforce_ep(position_id: str, body: MaxHoldTimePositionBody, user: dict = Depends(get_current_user)):  # noqa: B008
    return await crypto_mht_enforce(
        position_id=position_id, current_price=body.current_price,
        actor=(user.get("email") or "operator") + " · max_hold_time_guard",
        max_hold_minutes=body.max_hold_minutes,
    )


# ───────────────────── Position Monitor scheduler control surface ─────────────────────

@router.get("/monitor/status")
async def monitor_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.risk.position_monitor import get_status as _get_status  # noqa: WPS433
    return _get_status()


@router.post("/monitor/run-once")
async def monitor_run_once(
    user: dict = Depends(get_current_user),  # noqa: B008
):
    """Manually trigger one monitor cycle. Useful for tests / operators
    who want to evaluate all open positions immediately without waiting
    for the next scheduled tick."""
    from shared.risk.position_monitor import run_once  # noqa: WPS433
    return await run_once(actor=(user.get("email") or "operator") + " · monitor_manual")


@router.get("/monitor/recent-evaluations")
async def monitor_recent_evaluations(
    limit: int = 50,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Recent rows from the monitor's evaluation log so the operator can
    see which guards fired (or held) on which positions and why."""
    from db import db  # noqa: WPS433
    from namespaces import RISK_MONITOR_EVALUATIONS  # noqa: WPS433
    rows = await db[RISK_MONITOR_EVALUATIONS].find({}, {"_id": 0}).sort("ts", -1).to_list(limit)
    return {"items": rows, "count": len(rows)}
