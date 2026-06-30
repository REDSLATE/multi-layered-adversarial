"""Trader admin routes — operator visibility for the sidecar trader.

Doctrine (2026-06-30, Path 2):
    MC = eyes only
    Trader = authority

These endpoints expose the trader's truth to MC's UI so the operator
can see what the sidecar is doing without touching Mongo directly.

Endpoints:
    GET  /api/admin/trader/status     — task alive? last cycle? env config?
    GET  /api/admin/trader/receipts   — last N trader_receipts (per-cycle tape)
    GET  /api/admin/trader/executions — last N executions where source=trader
    POST /api/admin/trader/seed-seats — idempotent: writes the operator's
                                        angel-name + brain pairings into
                                        seat_registry. Safe to call multiple
                                        times. Use this once after deploy
                                        to materialize the assignments.

All read endpoints are admin-gated. The trader has no operator-facing
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

from auth import get_current_user
from db import db


logger = logging.getLogger("risedual.admin.trader")
router = APIRouter(prefix="/admin/trader", tags=["admin", "trader"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/status")
async def trader_status(_: dict = Depends(get_current_user)) -> dict:
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
    _: dict = Depends(get_current_user),
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
    _: dict = Depends(get_current_user),
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


# Operator-canonical angel→brain pairings for the trader.
# Documented in /app/trader/seat.py::DEFAULT_SEATS. Repeated here so
# the seed endpoint can write them without importing the trader
# module (decoupled from trader's lifecycle).
_OPERATOR_SEAT_PAIRINGS = [
    # Equity lane
    {"lane": "equity", "role": "strategist", "angel": "Raziel",  "holder": "camino"},
    {"lane": "equity", "role": "governor",   "angel": "Nuriel",  "holder": "hellcat",
     "risk_multiplier": 1.0},
    {"lane": "equity", "role": "executor",   "angel": "Paschar", "holder": "gto"},
    {"lane": "equity", "role": "auditor",    "angel": "Sariel",  "holder": "barracuda"},
    # Crypto lane
    {"lane": "crypto", "role": "strategist", "angel": "Remiel",  "holder": "hellcat"},
    {"lane": "crypto", "role": "governor",   "angel": "Cassiel", "holder": "camino",
     "risk_multiplier": 1.0},
    {"lane": "crypto", "role": "executor",   "angel": "Israfel", "holder": "gto"},
    {"lane": "crypto", "role": "auditor",    "angel": "Zadkiel", "holder": "barracuda"},
]


@router.post("/seed-seats")
async def trader_seed_seats(actor: dict = Depends(get_current_user)) -> dict:
    """Idempotent seat-registry seeder.

    Writes the operator-canonical angel→brain pairings into the
    `seat_registry` collection. Safe to call repeatedly — uses upsert
    semantics with `$set` so an existing row's other fields (like
    `last_changed_at` audit) are preserved.

    Use this after a fresh deploy to materialize the assignments so
    MC's seat tile shows them. The trader itself doesn't NEED this
    call (it has DEFAULT_SEATS as a fallback), but the operator
    benefits from having the canonical assignments visible in
    Mongo for the seat tile + audit log.
    """
    now = _now_iso()
    results = []
    for p in _OPERATOR_SEAT_PAIRINGS:
        sid = f"{p['lane']}:{p['role']}"
        set_fields = {
            "lane": p["lane"],
            "role": p["role"],
            "angel": p["angel"],
            "holder": p["holder"],
            "assigned_by": (actor.get("email") or "operator-seed"),
            "reason": "seeded_by_admin_trader_seed_seats",
            "last_changed_at": now,
            "since": now,
        }
        if "risk_multiplier" in p:
            set_fields["risk_multiplier"] = p["risk_multiplier"]
        await db["seat_registry"].update_one(
            {"_id": sid},
            {"$set": set_fields, "$setOnInsert": {"_id": sid}},
            upsert=True,
        )
        results.append({
            "id": sid,
            "angel": p["angel"],
            "role": p["role"],
            "lane": p["lane"],
            "holder": p["holder"],
        })
    return {
        "ok": True,
        "applied_at": now,
        "applied_by": actor.get("email"),
        "count": len(results),
        "seats": results,
    }
