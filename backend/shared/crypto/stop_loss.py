"""Crypto-lane stop-loss lifecycle. Mirror of equity/stop_loss.py."""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from db import db
from namespaces import SHARED_LIVE_POSITIONS
from shared.live_positions import STATE_CLOSED, close
from shared.risk.stop_loss_guard import StopLossVerdict, stop_loss_guard


async def evaluate_position(
    *,
    position_id: str,
    current_price: float,
    stop_loss_pct: float = 2.0,
) -> dict:
    pos = await db[SHARED_LIVE_POSITIONS].find_one({"position_id": position_id}, {"_id": 0})
    if not pos:
        raise HTTPException(status_code=404, detail=f"position {position_id} not found")
    if (pos.get("lane") or "").lower() != "crypto":
        raise HTTPException(
            status_code=400,
            detail=f"position {position_id} is lane={pos.get('lane')!r}, not crypto",
        )
    if pos.get("state") == STATE_CLOSED:
        raise HTTPException(status_code=409, detail=f"position {position_id} is closed")

    side = "SHORT" if (pos.get("direction") or "long").lower() == "short" else "LONG"
    entry_price = _entry_price_from_position(pos)
    if entry_price is None or entry_price <= 0:
        raise HTTPException(status_code=400, detail="no usable entry price on position open fill")

    verdict = stop_loss_guard(
        side=side,
        entry_price=entry_price,
        current_price=float(current_price),
        stop_loss_pct=stop_loss_pct,
    )
    return _verdict_payload(pos, side, entry_price, current_price, verdict)


async def enforce_position(
    *,
    position_id: str,
    current_price: float,
    actor: str = "stop_loss_guard",
    stop_loss_pct: float = 2.0,
) -> dict:
    eval_payload = await evaluate_position(
        position_id=position_id,
        current_price=current_price,
        stop_loss_pct=stop_loss_pct,
    )
    verdict = eval_payload["verdict"]
    if verdict["action"] == "HOLD":
        return {"acted": False, **eval_payload}

    pos = await db[SHARED_LIVE_POSITIONS].find_one({"position_id": position_id}, {"_id": 0})
    if not pos:
        raise HTTPException(status_code=404, detail=f"position {position_id} disappeared mid-evaluate")

    side_mult = -1.0 if pos.get("direction") == "short" else 1.0
    pnl_pct = float(verdict["pnl_pct"])
    cur_notional = float(pos.get("current_notional_usd") or 0.0)
    pnl_usd = cur_notional * (pnl_pct / 100.0) * side_mult
    result = await close(
        position_id=position_id,
        actor=actor,
        pnl_usd=pnl_usd,
        pnl_pct=pnl_pct,
        outcome_label="stopped_out",
        note=f"[stop_loss_guard] {verdict['reason']}",
    )
    return {"acted": True, "action": "CLOSE", "position": result, **eval_payload}


def _entry_price_from_position(pos: dict) -> Optional[float]:
    for f in pos.get("fills") or []:
        if f.get("kind") == "open" and f.get("price"):
            try:
                return float(f["price"])
            except (TypeError, ValueError):
                return None
    return None


def _verdict_payload(pos, side, entry_price, current_price, verdict: StopLossVerdict) -> dict:
    return {
        "lane": "crypto",
        "guard": "stop_loss",
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
