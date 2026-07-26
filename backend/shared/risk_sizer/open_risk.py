"""Portfolio open-risk accounting — memory-resident, hot-path safe.

open risk = Σ planned remaining loss of live exit plans
          + Σ pending entry reservations (sized this pulse but not yet
            confirmed) — without these, concurrent evaluations would
            each consume the same remaining risk budget.
"""
from __future__ import annotations

import time
from typing import Any

_PENDING_TTL_S = 120.0
_pending: dict[str, dict[str, Any]] = {}   # intent_id → {lane, risk, notional, at}


def reset_for_tests() -> None:
    _pending.clear()


def _prune() -> None:
    now = time.monotonic()
    stale = [k for k, v in _pending.items() if now - v["at"] > _PENDING_TTL_S]
    for k in stale:
        _pending.pop(k, None)


def reserve_pending(intent_id: str, lane: str, risk_usd: float, notional_usd: float) -> None:
    _prune()
    _pending[intent_id] = {"lane": lane, "risk": float(risk_usd),
                           "notional": float(notional_usd), "at": time.monotonic()}


def release_pending(intent_id: str) -> None:
    _pending.pop(intent_id, None)


def pending_risk(lane: str) -> float:
    _prune()
    return sum(v["risk"] for v in _pending.values() if v["lane"] == lane)


def open_plan_risk(lane: str) -> float:
    """Remaining planned loss across live exit plans, from the
    canonical persisted stop (long: (entry-stop)×qty)."""
    from shared.hotpath import exit_plans  # noqa: WPS433
    total = 0.0
    for p in exit_plans.load_live(lane):
        entry = float(p.get("entry_price") or 0.0)
        stop = float(p.get("stop_price") or 0.0)
        qty = float(p.get("qty_held") or 0.0)
        side = (p.get("side") or "long").lower()
        if side == "short":
            total += max(0.0, stop - entry) * qty
        else:
            total += max(0.0, entry - stop) * qty
    return total


def total_open_risk(lane: str) -> float:
    return open_plan_risk(lane) + pending_risk(lane)
