"""Hard exposure caps — code-level rails enforced on every order route.

Doctrine (Week 1 paper):
  * $10  per order  — notional cap on a single intent's order
  * $50  per day    — sum of executed order notional in the rolling 24h window
  * $100 open notional — total live market value across open positions

These caps are SOFTWARE — there is no operator UI to relax them. To
loosen them you change the constants here and redeploy. That's
deliberate: caps are battle-tested in paper so they're proven by the
time live trading lands.

Caps are evaluated by the gate chain BEFORE the broker is touched.
Failure raises `CapExceeded`, which the chain turns into a blocking gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from db import db
from namespaces import EXECUTION_RECEIPTS
from shared.broker.alpaca_routes import get_alpaca_adapter


# Hard-coded paper-trading rails. Change here = redeploy.
CAP_PER_ORDER_USD: float = 10.0
CAP_PER_DAY_USD: float = 50.0
CAP_OPEN_NOTIONAL_USD: float = 100.0


class CapExceeded(Exception):
    """Raised when a planned order would breach a hard cap."""


@dataclass
class CapEvaluation:
    name: str
    cap_usd: float
    current_usd: float
    projected_usd: float
    passed: bool
    reason: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def daily_spend_usd(window_hours: int = 24) -> float:
    """Sum of executed order notional in the last `window_hours`."""
    since = (_now() - timedelta(hours=window_hours)).isoformat()
    cursor = db[EXECUTION_RECEIPTS].find(
        {"executed_at": {"$gte": since}, "side": {"$in": ["BUY", "SELL"]}},
        {"_id": 0, "notional_usd": 1, "side": 1},
    )
    total = 0.0
    async for row in cursor:
        # Treat BUY notional as "spend" and SELL notional as "spend" too —
        # we cap *trading throughput* per day, not just net inflow.
        total += float(row.get("notional_usd") or 0.0)
    return total


async def open_notional_usd() -> float:
    """Sum of |market_value| across live positions at the broker. Returns
    0.0 if no broker is connected (we still let the per-order/per-day
    caps work in dry-run mode)."""
    adapter = await get_alpaca_adapter()
    if not adapter:
        return 0.0
    try:
        positions = await adapter.list_positions()
    except Exception:  # noqa: BLE001
        return 0.0
    return sum(abs(float(p.get("market_value") or 0.0)) for p in positions)


def evaluate_per_order(order_notional_usd: float) -> CapEvaluation:
    passed = order_notional_usd <= CAP_PER_ORDER_USD
    return CapEvaluation(
        name="cap_per_order",
        cap_usd=CAP_PER_ORDER_USD,
        current_usd=0.0,
        projected_usd=order_notional_usd,
        passed=passed,
        reason=(
            f"order notional ${order_notional_usd:.2f} ≤ cap ${CAP_PER_ORDER_USD:.2f}"
            if passed else
            f"order notional ${order_notional_usd:.2f} exceeds per-order cap ${CAP_PER_ORDER_USD:.2f}"
        ),
    )


async def evaluate_daily(order_notional_usd: float) -> CapEvaluation:
    spent = await daily_spend_usd()
    projected = spent + order_notional_usd
    passed = projected <= CAP_PER_DAY_USD
    return CapEvaluation(
        name="cap_per_day",
        cap_usd=CAP_PER_DAY_USD,
        current_usd=spent,
        projected_usd=projected,
        passed=passed,
        reason=(
            f"24h spend ${spent:.2f} + new ${order_notional_usd:.2f} = "
            f"${projected:.2f} ≤ cap ${CAP_PER_DAY_USD:.2f}"
            if passed else
            f"24h spend ${spent:.2f} + new ${order_notional_usd:.2f} = "
            f"${projected:.2f} would exceed daily cap ${CAP_PER_DAY_USD:.2f}"
        ),
    )


async def evaluate_open_notional(order_notional_usd: float, side: str) -> CapEvaluation:
    """Only BUY orders grow open notional. SELL/COVER reduce it (and we
    don't need to gate them on this cap)."""
    current = await open_notional_usd()
    side_u = (side or "").upper()
    is_opening = side_u in ("BUY", "SHORT")
    projected = current + (order_notional_usd if is_opening else 0.0)
    passed = projected <= CAP_OPEN_NOTIONAL_USD
    return CapEvaluation(
        name="cap_open_notional",
        cap_usd=CAP_OPEN_NOTIONAL_USD,
        current_usd=current,
        projected_usd=projected,
        passed=passed,
        reason=(
            f"open notional ${current:.2f}"
            + (f" + new ${order_notional_usd:.2f}" if is_opening else "")
            + f" = ${projected:.2f} ≤ cap ${CAP_OPEN_NOTIONAL_USD:.2f}"
            if passed else
            f"open notional ${current:.2f} + new ${order_notional_usd:.2f}"
            f" = ${projected:.2f} would exceed open-notional cap ${CAP_OPEN_NOTIONAL_USD:.2f}"
        ),
    )


async def evaluate_all(order_notional_usd: float, side: str) -> list[CapEvaluation]:
    """Run every cap check. Returns ordered list of CapEvaluation."""
    return [
        evaluate_per_order(order_notional_usd),
        await evaluate_daily(order_notional_usd),
        await evaluate_open_notional(order_notional_usd, side),
    ]


def caps_snapshot() -> dict:
    return {
        "per_order_usd": CAP_PER_ORDER_USD,
        "per_day_usd": CAP_PER_DAY_USD,
        "open_notional_usd": CAP_OPEN_NOTIONAL_USD,
    }
