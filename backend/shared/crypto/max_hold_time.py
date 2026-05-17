"""Crypto-lane max-hold-time lifecycle. Mirror of equity/max_hold_time.py."""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from db import db
from namespaces import SHARED_LIVE_POSITIONS
from shared.live_positions import STATE_CLOSED, close
from shared.risk.max_hold_time_guard import MaxHoldVerdict, max_hold_time_guard


async def evaluate_position(
    *,
    position_id: str,
    current_price: Optional[float] = None,
    max_hold_minutes: float = 60.0 * 24.0,
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

    verdict = max_hold_time_guard(
        opened_at=pos.get("opened_at") or "",
        max_hold_minutes=max_hold_minutes,
    )
    return _verdict_payload(pos, current_price, verdict)


async def enforce_position(
    *,
    position_id: str,
    current_price: Optional[float] = None,
    actor: str = "max_hold_time_guard",
    max_hold_minutes: float = 60.0 * 24.0,
) -> dict:
    eval_payload = await evaluate_position(
        position_id=position_id,
        current_price=current_price,
        max_hold_minutes=max_hold_minutes,
    )
    verdict = eval_payload["verdict"]
    if verdict["action"] == "HOLD":
        return {"acted": False, **eval_payload}

    pos = await db[SHARED_LIVE_POSITIONS].find_one({"position_id": position_id}, {"_id": 0})
    if not pos:
        raise HTTPException(status_code=404, detail=f"position {position_id} disappeared mid-evaluate")

    entry_price = _entry_price_from_position(pos) or 0.0
    side_mult = -1.0 if pos.get("direction") == "short" else 1.0
    cur_notional = float(pos.get("current_notional_usd") or 0.0)
    pnl_pct: Optional[float] = None
    pnl_usd: Optional[float] = None
    if entry_price > 0 and current_price is not None and current_price > 0:
        if pos.get("direction") == "short":
            pnl_pct = ((entry_price - float(current_price)) / entry_price) * 100.0
        else:
            pnl_pct = ((float(current_price) - entry_price) / entry_price) * 100.0
        pnl_usd = cur_notional * (pnl_pct / 100.0) * side_mult

    if pnl_usd is None:
        outcome_label = "scratch"
    elif pnl_usd > 0:
        outcome_label = "win"
    elif pnl_usd < 0:
        outcome_label = "loss"
    else:
        outcome_label = "scratch"

    result = await close(
        position_id=position_id,
        actor=actor,
        pnl_usd=pnl_usd,
        pnl_pct=round(pnl_pct, 4) if pnl_pct is not None else None,
        outcome_label=outcome_label,
        note=f"[max_hold_time_guard] {verdict['reason']}",
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


def _verdict_payload(pos, current_price, verdict: MaxHoldVerdict) -> dict:
    return {
        "lane": "crypto",
        "guard": "max_hold_time",
        "position_id": pos["position_id"],
        "symbol": pos.get("symbol"),
        "opened_at": pos.get("opened_at"),
        "current_price": float(current_price) if current_price is not None else None,
        "verdict": {
            "action": verdict.action,
            "reason": verdict.reason,
            "held_for_minutes": verdict.held_for_minutes,
            "target_minutes": verdict.target_minutes,
            "close_fraction": verdict.close_fraction,
        },
    }
