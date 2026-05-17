"""Equity-lane trailing-stop lifecycle.

Stateful: persists the running peak_price on the position doc so the
guard remembers the high-water (LONG) / low-water (SHORT) across ticks.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

from db import db
from namespaces import SHARED_LIVE_POSITIONS
from shared.live_positions import STATE_CLOSED, close
from shared.risk.trailing_stop_guard import TrailingStopVerdict, trailing_stop_guard


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def evaluate_position(
    *,
    position_id: str,
    current_price: float,
    trail_pct: float = 1.5,
    activate_after_pct: float = 1.0,
    persist_peak: bool = True,
) -> dict:
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
        raise HTTPException(status_code=400, detail="no usable entry price on position open fill")

    previous_peak = pos.get("peak_price")
    verdict = trailing_stop_guard(
        side=side,
        entry_price=entry_price,
        current_price=float(current_price),
        previous_peak=previous_peak,
        trail_pct=trail_pct,
        activate_after_pct=activate_after_pct,
    )

    # Persist the updated peak so the next tick sees today's high-water.
    if persist_peak and verdict.new_peak and verdict.new_peak != previous_peak:
        await db[SHARED_LIVE_POSITIONS].update_one(
            {"position_id": position_id},
            {"$set": {"peak_price": float(verdict.new_peak), "peak_updated_at": _now_iso()}},
        )

    return _verdict_payload(pos, side, entry_price, current_price, verdict)


async def enforce_position(
    *,
    position_id: str,
    current_price: float,
    actor: str = "trailing_stop_guard",
    trail_pct: float = 1.5,
    activate_after_pct: float = 1.0,
) -> dict:
    eval_payload = await evaluate_position(
        position_id=position_id,
        current_price=current_price,
        trail_pct=trail_pct,
        activate_after_pct=activate_after_pct,
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
    # Reconstruct pnl_pct from entry vs current (verdict's pnl is from
    # peak — informative, but trade pnl is from entry).
    if entry_price > 0:
        if pos.get("direction") == "short":
            pnl_pct = ((entry_price - float(current_price)) / entry_price) * 100.0
        else:
            pnl_pct = ((float(current_price) - entry_price) / entry_price) * 100.0
    else:
        pnl_pct = 0.0
    pnl_usd = cur_notional * (pnl_pct / 100.0) * side_mult
    result = await close(
        position_id=position_id,
        actor=actor,
        pnl_usd=pnl_usd,
        pnl_pct=round(pnl_pct, 4),
        outcome_label="win" if pnl_usd > 0 else ("loss" if pnl_usd < 0 else "scratch"),
        note=f"[trailing_stop_guard] {verdict['reason']}",
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


def _verdict_payload(pos, side, entry_price, current_price, verdict: TrailingStopVerdict) -> dict:
    return {
        "lane": "equity",
        "guard": "trailing_stop",
        "position_id": pos["position_id"],
        "symbol": pos.get("symbol"),
        "side": side,
        "entry_price": entry_price,
        "current_price": float(current_price),
        "verdict": {
            "action": verdict.action,
            "reason": verdict.reason,
            "new_peak": verdict.new_peak,
            "pnl_from_peak_pct": verdict.pnl_from_peak_pct,
            "target_pct": verdict.target_pct,
            "close_fraction": verdict.close_fraction,
        },
    }
