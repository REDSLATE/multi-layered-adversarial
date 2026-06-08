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
# Per-lane caps — each lane's per-order ceiling lives in its own
# subpackage so a crypto-only change doesn't touch the equity tree.
# (2026-02-16 reorg.)
from shared.crypto.exposure_caps import CRYPTO_PER_ORDER_USD as _CRYPTO_PER_ORDER_USD


import os


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# Paper-trading rails. Change here = redeploy — OR set the env override
# below for live-pilot tightening without a redeploy.
#
# 2026-05-14: Caps lifted for paper-trading rollout. Operator confirmed
# the brains should trade freely on paper. The cap STRUCTURE stays in
# place so it can be tightened the day we move toward live trading.
#
# 2026-06-07 (live $500 pilot): env overrides added so the operator
# can ratchet caps DOWN live without touching code:
#   RISEDUAL_CAP_PER_ORDER_USD   — single-order ceiling
#   RISEDUAL_CAP_PER_DAY_USD     — rolling-24h spend ceiling
#   RISEDUAL_CAP_OPEN_NOTIONAL_USD — total open notional ceiling
#   RISEDUAL_CAP_PER_ORDER_EQUITY_USD — per-lane override (equity)
#   RISEDUAL_CAP_PER_ORDER_CRYPTO_USD — per-lane override (crypto)
# Doctrine: env can only TIGHTEN, never loosen — but enforcement of
# that invariant is operator discipline, not code. Pick low values.
CAP_PER_ORDER_USD: float = _env_float("RISEDUAL_CAP_PER_ORDER_USD", 100_000.0)
CAP_PER_DAY_USD: float = _env_float("RISEDUAL_CAP_PER_DAY_USD", 1_000_000.0)
CAP_OPEN_NOTIONAL_USD: float = _env_float("RISEDUAL_CAP_OPEN_NOTIONAL_USD", 1_000_000.0)

# Per-lane override. Set entries to None for "use the global cap".
# These overrides apply to the per-order cap only — day/open caps
# still use the globals above.
#
# Doctrine pin (2026-06-07): only ADD a lane to this dict when the
# operator explicitly sets `RISEDUAL_CAP_PER_ORDER_<LANE>_USD`. The
# gate-chain emits a gate named `cap_per_order_<lane>` ONLY when the
# lane appears here, otherwise the canonical `cap_per_order` gate
# applies. Implicitly mirroring the global cap into every lane
# would rename gates for tests + dashboards.
CAP_PER_ORDER_BY_LANE: dict[str, float] = {
    "crypto": _env_float(
        "RISEDUAL_CAP_PER_ORDER_CRYPTO_USD", _CRYPTO_PER_ORDER_USD,
    ),
}
if os.environ.get("RISEDUAL_CAP_PER_ORDER_EQUITY_USD"):
    CAP_PER_ORDER_BY_LANE["equity"] = _env_float(
        "RISEDUAL_CAP_PER_ORDER_EQUITY_USD", CAP_PER_ORDER_USD,
    )


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


def evaluate_per_order(order_notional_usd: float, lane: Optional[str] = None) -> CapEvaluation:
    cap = CAP_PER_ORDER_BY_LANE.get(lane or "", CAP_PER_ORDER_USD)
    passed = order_notional_usd <= cap
    label = f"cap_per_order_{lane}" if lane in CAP_PER_ORDER_BY_LANE else "cap_per_order"
    return CapEvaluation(
        name=label,
        cap_usd=cap,
        current_usd=0.0,
        projected_usd=order_notional_usd,
        passed=passed,
        reason=(
            f"order notional ${order_notional_usd:.2f} ≤ {label} cap ${cap:.2f}"
            if passed else
            f"order notional ${order_notional_usd:.2f} exceeds {label} cap ${cap:.2f}"
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


async def evaluate_all(order_notional_usd: float, side: str, lane: Optional[str] = None) -> list[CapEvaluation]:
    """Run every cap check. Returns ordered list of CapEvaluation."""
    return [
        evaluate_per_order(order_notional_usd, lane=lane),
        await evaluate_daily(order_notional_usd),
        await evaluate_open_notional(order_notional_usd, side),
    ]


def caps_snapshot() -> dict:
    """Single source of truth for exposure caps. Returns globals plus
    per-lane overrides so UI / Mission Control / RoadGuard all read the
    same numbers. Adding a new lane override propagates everywhere
    without UI changes."""
    return {
        "per_order_usd": CAP_PER_ORDER_USD,
        "per_day_usd": CAP_PER_DAY_USD,
        "open_notional_usd": CAP_OPEN_NOTIONAL_USD,
        "per_order_by_lane_usd": dict(CAP_PER_ORDER_BY_LANE),
    }


def cap_for_lane(lane: Optional[str]) -> float:
    """Resolve the effective per-order cap for `lane`. Falls back to
    the global per-order cap when no lane override exists."""
    if lane and lane in CAP_PER_ORDER_BY_LANE:
        return CAP_PER_ORDER_BY_LANE[lane]
    return CAP_PER_ORDER_USD
