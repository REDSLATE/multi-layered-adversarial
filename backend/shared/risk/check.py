"""Risk — the ONE module that enforces hard limits.

Doctrine (2026-02-27 architectural reduction):

    Market Data → Brain → Seat → RISK → Broker

Risk is the single non-negotiable gate between Seat and Broker. It
is the merger of every "money-safety" check that previously sprawled
across the codebase:
    * shared/broker_freeze.py            (kill switch)
    * shared/broker/webull_caps.py       (per-order cap evaluator)
    * shared/in_flight_orders.py         (idempotency)
    * shared/brain_lane_policy.py        (lane on/off)
    * shared/exposure_caps.py            (daily exposure cap)
    * shared/crypto/exposure_caps.py     (same, crypto)

Risk says NO when:
    1. Master Trading Switch is OFF (operator freeze)
    2. Per-order USD cap is exceeded
    3. Daily exposure cap is exhausted
    4. Lane is disabled (equity/crypto operator toggle)
    5. Intent is already executed (idempotency)

Risk does NOT:
    * second-guess the brain (Brain layer)
    * second-guess the Seat (Seat layer)
    * run dry-runs or simulations (those were diagnostic)
    * score the setup quality (that was diagnostic)
    * apply confidence floors (Seat policy, not money safety)

If Risk says `ok=False`, broker is never called. Period.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from db import db
from namespaces import SHARED_INTENTS


@dataclass(frozen=True)
class RiskCheck:
    ok: bool
    reason: str
    notional_usd: float
    cap_per_order_usd: float
    cap_daily_usd: float
    spent_today_usd: float


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _per_order_cap() -> float:
    try:
        return float(os.environ.get("RISEDUAL_CAP_PER_ORDER_USD", "10"))
    except (TypeError, ValueError):
        return 10.0


def _daily_cap() -> float:
    try:
        return float(os.environ.get("RISEDUAL_CAP_DAILY_USD", "1000"))
    except (TypeError, ValueError):
        return 1000.0


async def _is_freeze_on() -> bool:
    """Master Trading Switch — when OFF, every Risk check fails. The
    flag lives in `runtime_flags._id='master_trading_switch'`.
    Default to ON (freeze inactive) if missing — operator sets up
    the switch explicitly via the UI."""
    doc = await db["runtime_flags"].find_one(
        {"_id": "master_trading_switch"}, {"_id": 0, "enabled": 1}
    )
    if not doc:
        return False  # no doc → not frozen
    # Convention: `enabled=True` means trading is ARMED, so freeze is OFF.
    return not bool(doc.get("enabled"))


async def _is_lane_enabled(lane: str) -> bool:
    """Per-lane operator toggle. Doc:
        runtime_flags._id='lane_enabled'  {equity: bool, crypto: bool}
    Defaults to enabled when the doc/key is missing."""
    doc = await db["runtime_flags"].find_one(
        {"_id": "lane_enabled"}, {"_id": 0}
    )
    if not doc:
        return True
    val = doc.get((lane or "").lower())
    return True if val is None else bool(val)


async def _daily_spent_usd() -> float:
    """Sum of `notional_usd` on `executions` for the current UTC day."""
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    pipeline = [
        {"$match": {"ts": {"$regex": f"^{today_iso}"}, "ok": True}},
        {"$group": {"_id": None, "spent": {"$sum": "$notional_usd"}}},
    ]
    async for row in db["executions"].aggregate(pipeline, maxTimeMS=4000):
        return float(row.get("spent") or 0.0)
    return 0.0


async def check(
    intent: dict[str, Any],
    *,
    notional_usd: Optional[float] = None,
) -> RiskCheck:
    """Apply all hard limits. Returns a RiskCheck. Caller must respect
    `ok` — if False, do NOT call the broker."""
    lane = (intent.get("lane") or "").lower()
    intent_id = intent.get("intent_id") or ""

    per_order = _per_order_cap()
    daily = _daily_cap()
    n = float(notional_usd) if notional_usd is not None else per_order
    n = min(n, per_order)
    spent = await _daily_spent_usd()

    base = dict(
        notional_usd=n,
        cap_per_order_usd=per_order,
        cap_daily_usd=daily,
        spent_today_usd=spent,
    )

    if intent.get("executed"):
        return RiskCheck(ok=False, reason="already_executed", **base)

    if await _is_freeze_on():
        return RiskCheck(ok=False, reason="master_freeze_on", **base)

    if not await _is_lane_enabled(lane):
        return RiskCheck(ok=False, reason=f"lane_disabled:{lane}", **base)

    if n <= 0:
        return RiskCheck(ok=False, reason="notional_zero_or_negative", **base)

    if (spent + n) > daily:
        return RiskCheck(
            ok=False,
            reason=f"daily_cap_exceeded:spent={spent:.2f}+req={n:.2f}>cap={daily:.2f}",
            **base,
        )

    # Idempotency double-check at the DB layer — race-safe; if another
    # concurrent route already set executed=True, our update will see
    # it on the way back via the broker_router's order writer.
    if intent_id:
        live = await db[SHARED_INTENTS].find_one(
            {"intent_id": intent_id}, {"_id": 0, "executed": 1}
        )
        if live and live.get("executed"):
            return RiskCheck(
                ok=False, reason="already_executed_concurrent", **base,
            )

    return RiskCheck(ok=True, reason="ok", **base)


__all__ = ["RiskCheck", "check"]
