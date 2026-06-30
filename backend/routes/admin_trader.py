"""Trader admin routes — operator visibility for the sidecar trader.

Doctrine (2026-06-30, Path 2):
    MC = eyes only
    Trader = authority

These endpoints expose the trader's truth to MC's UI so the operator
can see what the sidecar is doing without touching Mongo directly.

Endpoints:
    GET /api/admin/trader/status     — task alive? last cycle? env config?
    GET /api/admin/trader/receipts   — last N trader_receipts (per-cycle tape)
    GET /api/admin/trader/executions — last N executions where source=trader

All endpoints are read-only. The trader has no operator-facing
controls in v1 — it runs on env vars + the seat_registry. To halt
it, the operator either sets TRADER_ENABLED=false and redeploys,
or flips master_trading_switch to disarmed (the trader's Risk
module will block every trade).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import require_admin
from db import db


logger = logging.getLogger("risedual.admin.trader")
router = APIRouter(prefix="/api/admin/trader", tags=["admin", "trader"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/status")
async def trader_status(_: dict = Depends(require_admin)) -> dict:
    """Operator visibility: is the trader alive and ticking?"""
    enabled = os.environ.get("TRADER_ENABLED", "false").lower() == "true"
    broker_disabled = os.environ.get("BROKER_DISABLED", "false").lower() == "true"
    auto_router_off = os.environ.get("AUTO_ROUTER_ENABLED", "true").lower() == "false"

    # Latest receipt = proxy for "loop is ticking".
    last_receipt = await db["trader_receipts"].find_one(
        {"source": "trader"}, sort=[("ts", -1)], projection={"_id": 0}
    )
    # Receipt count last 5 min — sanity check the loop is firing on schedule.
    five_min_ago = datetime.now(timezone.utc).timestamp() - 300
    recent_count = await db["trader_receipts"].count_documents(
        {"ts": {"$gte": datetime.fromtimestamp(
            five_min_ago, tz=timezone.utc).isoformat()
        }},
        maxTimeMS=4000,
    )
    last_execution = await db["executions"].find_one(
        {"source": "trader"}, sort=[("ts", -1)], projection={"_id": 0}
    )
    today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fires_today = await db["executions"].count_documents(
        {"source": "trader", "ok": True, "ts": {"$regex": f"^{today_prefix}"}},
        maxTimeMS=4000,
    )
    spent_today = 0.0
    pipeline = [
        {"$match": {
            "source": "trader", "ok": True,
            "ts": {"$regex": f"^{today_prefix}"},
        }},
        {"$group": {"_id": None, "spent": {"$sum": "$notional_usd"}}},
    ]
    async for row in db["executions"].aggregate(pipeline, maxTimeMS=4000):
        spent_today = float(row.get("spent") or 0.0)

    return {
        "ok": True,
        "env": {
            "TRADER_ENABLED": enabled,
            "BROKER_DISABLED": broker_disabled,
            "AUTO_ROUTER_DISABLED": auto_router_off,
            "interval_sec": int(os.environ.get("TRADER_INTERVAL_SEC", "60")),
            "per_order_cap_usd": float(os.environ.get("TRADER_PER_ORDER_USD_CAP", "10")),
            "daily_cap_usd": float(os.environ.get("TRADER_DAILY_USD_CAP", "1000")),
            "crypto_pair": os.environ.get("TRADER_CRYPTO_PAIR", "XBTUSD"),
            "equity_ticker": os.environ.get("TRADER_EQUITY_TICKER", "TSLA"),
            "confidence_threshold": float(
                os.environ.get("TRADER_CONFIDENCE_THRESHOLD", "0.55")
            ),
        },
        "loop": {
            "last_receipt_ts": (last_receipt or {}).get("ts"),
            "last_receipt_lane": (last_receipt or {}).get("lane"),
            "last_receipt_symbol": (last_receipt or {}).get("symbol"),
            "receipts_last_5_min": recent_count,
            "alive_inference": recent_count > 0,
        },
        "trades": {
            "fires_today": fires_today,
            "spent_today_usd": spent_today,
            "last_execution_ts": (last_execution or {}).get("ts"),
            "last_execution_lane": (last_execution or {}).get("lane"),
            "last_execution_action": (last_execution or {}).get("action"),
            "last_execution_broker": (last_execution or {}).get("broker"),
            "last_execution_ok": (last_execution or {}).get("ok"),
        },
        "checked_at": _now_iso(),
    }


@router.get("/receipts")
async def trader_receipts(
    _: dict = Depends(require_admin),
    limit: int = Query(default=50, ge=1, le=500),
    lane: Optional[str] = Query(default=None),
    fired_only: bool = Query(default=False),
) -> dict:
    """Most recent per-cycle receipts. Operator reads:
        what did the trader see this minute?
        what did each brain say?
        what did the seat doctrine pick?
        did risk block? did broker accept?
    """
    q: dict = {"source": "trader"}
    if lane:
        q["lane"] = lane.lower()
    if fired_only:
        q["chosen.verdict"] = {"$in": ["BUY", "SELL"]}
    cursor = (
        db["trader_receipts"]
        .find(q, {"_id": 0})
        .sort("ts", -1)
        .max_time_ms(8000)
    )
    rows = await cursor.to_list(limit)
    return {"ok": True, "count": len(rows), "items": rows}


@router.get("/executions")
async def trader_executions(
    _: dict = Depends(require_admin),
    limit: int = Query(default=50, ge=1, le=500),
    lane: Optional[str] = Query(default=None),
    ok: Optional[bool] = Query(default=None),
) -> dict:
    """Executions written by the trader. Only `source=trader` rows.

    Each row carries the broker_response or exception_msg — this is
    the long-awaited 'what did Kraken/Webull actually say?' tape.
    """
    q: dict = {"source": "trader"}
    if lane:
        q["lane"] = lane.lower()
    if ok is not None:
        q["ok"] = bool(ok)
    cursor = (
        db["executions"]
        .find(q, {"_id": 0})
        .sort("ts", -1)
        .max_time_ms(8000)
    )
    rows = await cursor.to_list(limit)
    return {"ok": True, "count": len(rows), "items": rows}
