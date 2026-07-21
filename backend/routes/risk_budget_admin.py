"""Daily budget admin — spent/cap visibility, RESET SPEND, cap knob.

2026-07-22 operator: "Where did the reset button go?" — the old
button (Intents page exposure-caps strip) was removed 2026-07-01 and
its endpoint wrote a marker the post-reduction risk gate never read.
This is the rebuild, wired to `shared/risk/check.py` (the ONE gate).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from db import db
from shared.risk.check import _daily_cap, _daily_cap_effective, _daily_spent_usd

router = APIRouter(prefix="/admin/risk/budget", tags=["risk-budget"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.get("")
async def budget_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    now = _now()
    day_end = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    reset_doc = await db["runtime_flags"].find_one(
        {"_id": "daily_spend_reset"}, {"_id": 0},
    ) or {}
    caps_doc = await db["runtime_flags"].find_one(
        {"_id": "risk_caps"}, {"_id": 0},
    ) or {}
    spent = await _daily_spent_usd()
    cap = await _daily_cap_effective()
    return {
        "spent_today_usd": round(spent, 2),
        "cap_daily_usd": cap,
        "cap_source": "override" if caps_doc.get("cap_daily_usd") is not None else "env",
        "cap_env_default": _daily_cap(),
        "remaining_usd": round(max(0.0, cap - spent), 2),
        "utc_day_resets_at": day_end.isoformat(),
        "resets_in_s": int((day_end - now).total_seconds()),
        "last_manual_reset_at": reset_doc.get("reset_at"),
        "last_manual_reset_by": reset_doc.get("reset_by"),
    }


@router.post("/reset")
async def reset_spend(_user: dict = Depends(get_current_user)):  # noqa: B008
    """RESET SPEND — spend tally restarts from now. Executions before
    this marker no longer count against today's cap."""
    now_iso = _now().isoformat()
    await db["runtime_flags"].update_one(
        {"_id": "daily_spend_reset"},
        {"$set": {"reset_at": now_iso,
                  "reset_by": _user.get("email") or "unknown"}},
        upsert=True,
    )
    return {"ok": True, "reset_at": now_iso,
            "spent_today_usd": round(await _daily_spent_usd(), 2)}


@router.post("/cap")
async def set_cap(
    body: dict,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Set the daily cap. null → revert to env default."""
    v = body.get("cap_daily_usd", "missing")
    if v == "missing":
        raise HTTPException(status_code=422, detail="cap_daily_usd required")
    if v is None:
        await db["runtime_flags"].update_one(
            {"_id": "risk_caps"}, {"$unset": {"cap_daily_usd": 1}},
        )
    else:
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="cap_daily_usd must be a number")
        if not (1.0 <= f <= 1_000_000.0):
            raise HTTPException(status_code=422, detail="cap_daily_usd out of range")
        await db["runtime_flags"].update_one(
            {"_id": "risk_caps"},
            {"$set": {"cap_daily_usd": f,
                      "updated_by": _user.get("email") or "unknown",
                      "updated_at": _now().isoformat()}},
            upsert=True,
        )
    return {"ok": True, "cap_daily_usd": await _daily_cap_effective()}
