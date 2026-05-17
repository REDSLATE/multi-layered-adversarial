"""Equity-lane take-profit lifecycle.

Wraps the lane-neutral `shared.risk.take_profit_guard` with equity
position bookkeeping. Belongs to Camaro's equity executor lane per
doctrine: executors enter, lifecycle guards exit, brains advise,
RoadGuard enforces.

Reads:    `shared_live_positions` (lane='equity' rows only)
Closes:   via `shared.live_positions.close()` — broadcasts to outcomes.
          Actual Alpaca close-order routing remains a separate piece
          to be wired alongside the Position Monitor loop.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from db import db
from namespaces import SHARED_LIVE_POSITIONS
from shared.live_positions import STATE_CLOSED, close, record_management
from shared.risk.take_profit_guard import TakeProfitVerdict, take_profit_guard


async def evaluate_position(
    *,
    position_id: str,
    current_price: float,
    take_profit_pct: float = 3.0,
    partial_take_pct: Optional[float] = None,
    partial_close_fraction: float = 0.50,
) -> dict:
    """Look up an equity live position, run the deterministic guard,
    return the verdict — does NOT execute any close. Use this from
    the operator UI / executor sidecar to preview.
    """
    pos = await db[SHARED_LIVE_POSITIONS].find_one({"position_id": position_id}, {"_id": 0})
    if not pos:
        raise HTTPException(status_code=404, detail=f"position {position_id} not found")
    if (pos.get("lane") or "").lower() != "equity":
        raise HTTPException(
            status_code=400,
            detail=f"position {position_id} is lane={pos.get('lane')!r}, not equity",
        )
    if pos.get("state") == STATE_CLOSED:
        raise HTTPException(status_code=409, detail=f"position {position_id} is closed")

    side = "SHORT" if (pos.get("direction") or "long").lower() == "short" else "LONG"
    entry_price = _entry_price_from_position(pos)
    if entry_price is None or entry_price <= 0:
        raise HTTPException(
            status_code=400,
            detail="no usable entry price on position open fill",
        )

    verdict = take_profit_guard(
        side=side,
        entry_price=entry_price,
        current_price=float(current_price),
        take_profit_pct=take_profit_pct,
        partial_take_pct=partial_take_pct,
        partial_close_fraction=partial_close_fraction,
    )
    return _verdict_payload(pos, side, entry_price, current_price, verdict)


async def enforce_position(
    *,
    position_id: str,
    current_price: float,
    actor: str = "take_profit_guard",
    take_profit_pct: float = 3.0,
    partial_take_pct: Optional[float] = None,
    partial_close_fraction: float = 0.50,
) -> dict:
    """Evaluate AND act. If the verdict is HOLD, no side effects. If
    REDUCE, calls `record_management` with a negative delta sized by
    `close_fraction`. If CLOSE, calls `close` and broadcasts an
    outcome. Brain advisory cannot override this path — that's the
    whole point of a deterministic guard.
    """
    eval_payload = await evaluate_position(
        position_id=position_id,
        current_price=current_price,
        take_profit_pct=take_profit_pct,
        partial_take_pct=partial_take_pct,
        partial_close_fraction=partial_close_fraction,
    )
    verdict = eval_payload["verdict"]
    action = verdict["action"]
    if action == "HOLD":
        return {"acted": False, **eval_payload}

    pos = await db[SHARED_LIVE_POSITIONS].find_one({"position_id": position_id}, {"_id": 0})
    if not pos:
        raise HTTPException(status_code=404, detail=f"position {position_id} disappeared mid-evaluate")

    if action == "REDUCE":
        cur_notional = float(pos.get("current_notional_usd") or 0.0)
        delta = -1.0 * cur_notional * float(verdict["close_fraction"])
        result = await record_management(
            position_id=position_id,
            actor=actor,
            note=f"[take_profit_guard] {verdict['reason']}",
            delta_notional_usd=delta,
        )
        return {"acted": True, "action": "REDUCE", "position": result, **eval_payload}

    # CLOSE
    side_mult = -1.0 if pos.get("direction") == "short" else 1.0
    entry_price = eval_payload["entry_price"]
    pnl_pct = float(verdict["pnl_pct"])
    cur_notional = float(pos.get("current_notional_usd") or 0.0)
    pnl_usd = cur_notional * (pnl_pct / 100.0) * side_mult
    result = await close(
        position_id=position_id,
        actor=actor,
        pnl_usd=pnl_usd,
        pnl_pct=pnl_pct,
        outcome_label="win" if pnl_usd > 0 else ("loss" if pnl_usd < 0 else "scratch"),
        note=f"[take_profit_guard] {verdict['reason']}",
    )
    return {"acted": True, "action": "CLOSE", "position": result, **eval_payload}


# ─── helpers ───

def _entry_price_from_position(pos: dict) -> Optional[float]:
    fills = pos.get("fills") or []
    for f in fills:
        if f.get("kind") == "open" and f.get("price"):
            try:
                return float(f["price"])
            except (TypeError, ValueError):
                return None
    return None


def _verdict_payload(
    pos: dict,
    side: str,
    entry_price: float,
    current_price: float,
    verdict: TakeProfitVerdict,
) -> dict:
    return {
        "lane": "equity",
        "position_id": pos["position_id"],
        "symbol": pos.get("symbol"),
        "side": side,
        "entry_price": entry_price,
        "current_price": float(current_price),
        "verdict": {
            "action": verdict.action,
            "reason": verdict.reason,
            "pnl_pct": verdict.pnl_pct,
            "target_pct": verdict.target_pct,
            "close_fraction": verdict.close_fraction,
        },
    }
