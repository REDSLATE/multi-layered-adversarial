"""MC helper — account-aware overlay between final notional and
`route_order()` (operator patch 2026-06, PATCH_POINT.py).

Full broker state is transient; only the compact `account_fit` dict is
persisted with the intent. Market confidence/edge is never touched.
"""
from __future__ import annotations

from typing import Any

from shared.account_context import get_account_snapshot
from shared.account_fit import evaluate_account_fit


async def apply_account_awareness(
    intent: dict[str, Any],
    *,
    requested_notional: float,
) -> tuple[dict[str, Any], float]:
    lane = str(intent.get("lane") or "").lower()
    symbol = str(intent.get("symbol") or "")
    action = str(intent.get("action") or intent.get("direction") or "")
    broker_override = intent.get("broker_override")

    snapshot = await get_account_snapshot(
        lane,
        broker_override=broker_override,
    )
    fit = evaluate_account_fit(
        snapshot=snapshot,
        symbol=symbol,
        action=action,
        requested_notional=requested_notional,
    )

    # Keep the market score untouched.
    out = dict(intent)
    out["account_fit"] = fit.compact()
    out["account_fit"]["broker"] = snapshot.broker
    out["account_fit"]["captured_at_ms"] = snapshot.captured_at_ms

    effective = round(max(0.0, requested_notional * fit.size_multiplier), 2)
    return out, effective
