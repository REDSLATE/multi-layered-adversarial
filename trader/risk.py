"""Risk — pre-trade hard limits. SQLite + in-memory only.

Doctrine pin (2026-07-01, operator directive):
    "No database before broker submit."

Mongo is NOT touched here. Every gate reads from either the
in-memory `state` cache (flags) or the local `store` (SQLite —
`daily_spent`, `already_executed`). Both are microsecond-scale
lookups; neither can hang.

Limits enforced (unchanged from prior contract):
    1. Master Trading Switch (state.master_switch_armed)
    2. Lane toggle (state.lane_enabled)
    3. Per-order USD cap (TRADER_PER_ORDER_USD_CAP)
    4. Daily USD cap (TRADER_DAILY_USD_CAP) — SUM(SQLite executions)
    5. Idempotency — `intent_id` PK check in SQLite

The `db` argument is accepted for signature compatibility with the
previous Mongo-backed version but is ignored. All state is local.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from trader import config, state, store


logger = logging.getLogger("trader.risk")


@dataclass(frozen=True)
class RiskVerdict:
    ok: bool
    reason: str
    notional_usd: float
    spent_today_usd: float


async def check(db, intent: dict, notional_usd: Optional[float] = None) -> RiskVerdict:
    lane = (intent.get("lane") or "").lower()
    intent_id = intent.get("intent_id") or ""
    per_order = config.per_order_cap_usd()
    daily = config.daily_cap_usd()

    requested = float(notional_usd) if notional_usd is not None else per_order
    n = min(requested, per_order)
    spent = store.daily_spent_usd()

    if store.already_executed(intent_id):
        return RiskVerdict(False, "already_executed", n, spent)
    if not state.master_switch_armed():
        return RiskVerdict(False, "master_switch_disarmed", n, spent)
    if not state.lane_enabled(lane):
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
