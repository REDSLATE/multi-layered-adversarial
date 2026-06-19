"""Admin endpoint — fire/inspect the Paradox MA-canary.

POST /api/admin/canary/fire    — one-shot canary intent
GET  /api/admin/canary/status  — env flag + recent canary outcomes

Both require admin JWT. The fire endpoint is the operator's
"manually prove the plumbing works" button — call it once with
`?symbol=BTC/USD&lane=crypto`, then trace the returned `intent_id`
to its receipt.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db
from namespaces import SHARED_INTENTS

from shared.strategies.canary_runner import (
    fire_canary as _fire_canary,
    is_canary_enabled,
)


router = APIRouter(prefix="/admin/canary", tags=["canary"])


@router.get("/status")
async def status(_user: dict = Depends(get_current_user)) -> dict:
    """Show the kill-switch state and the last 10 canary intents +
    their current gate_state. Lets the operator answer
    "is the canary actually flowing?" from one URL."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    recent = await db[SHARED_INTENTS].find(
        {"source": "ma_canary", "ingest_ts": {"$gte": cutoff}},
        {
            "_id": 0,
            "intent_id": 1,
            "symbol": 1,
            "lane": 1,
            "action": 1,
            "confidence": 1,
            "gate_state": 1,
            "executed": 1,
            "stack": 1,
            "ingest_ts": 1,
        },
    ).sort("ingest_ts", -1).limit(10).to_list(10)

    return {
        "enabled": is_canary_enabled(),
        "default_notional_usd": float(os.environ.get("PARADOX_MA_CANARY_NOTIONAL", "10")),
        "default_lane": os.environ.get("PARADOX_MA_CANARY_LANE", "equity"),
        "kill_switch_env_var": "PARADOX_MA_CANARY_ENABLED",
        "recent_canary_intents_24h": recent,
        "doctrine_note": (
            "Canary is for proving plumbing, not for tuning strategy. "
            "If the canary intent flows through to a broker receipt, "
            "the rest of the pipeline is wired. If it dies with a "
            "named gate, the gate name tells you what to fix next."
        ),
    }


@router.post("/fire")
async def fire(
    symbol: str = Query(..., description="e.g. AAPL or BTC/USD"),
    lane: str = Query(default="equity", regex="^(equity|crypto)$"),
    timeframe: str = Query(default="1h"),
    notional_usd: Optional[float] = Query(default=None, ge=0.01, le=10000.0),
    fast_window: int = Query(default=10, ge=2, le=200),
    slow_window: int = Query(default=30, ge=3, le=500),
    _user: dict = Depends(get_current_user),
) -> dict:
    """One-shot canary fire. Returns the signal + intent_id +
    next-step hint. Auto-router will route the intent on its next
    tick (≤30s). If you want to trace it, hit
    `GET /api/intents/{intent_id}/why` after a tick or two."""
    return await _fire_canary(
        symbol=symbol,
        lane=lane,  # type: ignore[arg-type]
        timeframe=timeframe,
        notional_usd=notional_usd,
        fast_window=fast_window,
        slow_window=slow_window,
    )
