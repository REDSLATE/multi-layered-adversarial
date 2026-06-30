"""Risk — pre-trade hard limits for the trader.

Single source of truth. No second-guessing. If `check` returns
ok=False, the broker is NOT called.

Limits enforced:
    1. Master Trading Switch (runtime_flags doc) — same flag MC uses,
       so the operator's "halt" button still works.
    2. Lane toggle (runtime_flags.lane_enabled) — same operator switch.
    3. Per-order USD cap (env: TRADER_PER_ORDER_USD_CAP, default $10).
    4. Daily USD cap (env: TRADER_DAILY_USD_CAP, default $1000) —
       sum of `notional_usd` on `executions` where ok=True today.
    5. Idempotency — same intent_id can't fire twice in the same day.
       (The trader generates intent_ids deterministically per cycle.)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from trader import config


logger = logging.getLogger("trader.risk")


@dataclass(frozen=True)
class RiskVerdict:
    ok: bool
    reason: str
    notional_usd: float
    spent_today_usd: float


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_prefix() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _master_switch_armed(db) -> bool:
    doc = await db["runtime_flags"].find_one(
        {"_id": "master_trading_switch"}, {"_id": 0, "enabled": 1}
    )
    if not doc:
        return False  # no doc → not armed (safer default for sidecar)
    return bool(doc.get("enabled"))


async def _lane_enabled(db, lane: str) -> bool:
    doc = await db["runtime_flags"].find_one(
        {"_id": "lane_enabled"}, {"_id": 0}
    )
    if not doc:
        return True
    val = doc.get((lane or "").lower())
    return True if val is None else bool(val)


async def _daily_spent_usd(db) -> float:
    pipeline = [
        {"$match": {
            "ts": {"$regex": f"^{_today_prefix()}"},
            "ok": True,
            "source": "trader",  # only sum the trader's own fills
        }},
        {"$group": {"_id": None, "spent": {"$sum": "$notional_usd"}}},
    ]
    async for row in db["executions"].aggregate(pipeline, maxTimeMS=4000):
        return float(row.get("spent") or 0.0)
    return 0.0


async def _already_executed(db, intent_id: str) -> bool:
    if not intent_id:
        return False
    doc = await db["executions"].find_one(
        {"intent_id": intent_id, "ok": True}, {"_id": 1}
    )
    return doc is not None


async def check(db, intent: dict, notional_usd: Optional[float] = None) -> RiskVerdict:
    lane = (intent.get("lane") or "").lower()
    intent_id = intent.get("intent_id") or ""
    per_order = config.per_order_cap_usd()
    daily = config.daily_cap_usd()

    requested = float(notional_usd) if notional_usd is not None else per_order
    n = min(requested, per_order)
    spent = await _daily_spent_usd(db)

    if await _already_executed(db, intent_id):
        return RiskVerdict(False, "already_executed", n, spent)
    if not await _master_switch_armed(db):
        return RiskVerdict(False, "master_switch_disarmed", n, spent)
    if not await _lane_enabled(db, lane):
        return RiskVerdict(False, f"lane_disabled:{lane}", n, spent)
    if n <= 0:
        return RiskVerdict(False, "notional_zero", n, spent)
    if (spent + n) > daily:
        return RiskVerdict(
            False,
            f"daily_cap_exceeded:spent={spent:.2f}+req={n:.2f}>cap={daily:.2f}",
            n, spent,
        )
    return RiskVerdict(True, "ok", n, spent)
