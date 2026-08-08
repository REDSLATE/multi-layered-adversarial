"""Promotion gate (2026-08-05 operator directive).

Nothing returns to automated live entry because tests pass or the
software functions. A strategy EARNS deployment on forward-recorded
signals (shadow fills scored by the missed-entry ledger while the
system sits in exit_only):
  · enough completed observations        (n ≥ min_n, per lane)
  · positive expectancy after est. costs (mean return − cost > 0)
  · profit factor above threshold
  · controlled max drawdown of the counterfactual equity curve
  · no dependence on one unusually profitable trade
Each lane must pass on its own. Knobs in
`runtime_flags._id=promotion_gate`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

FLAG_ID = "promotion_gate"
DEFAULTS: dict[str, Any] = {
    "min_n": 30,
    "min_profit_factor": 1.1,
    "max_drawdown_pct_points": 10.0,
    "max_single_trade_share": 0.5,
    "cost_pct": 0.30,
    "window_days": 30,
}


async def get_gate_config() -> dict:
    from db import db  # noqa: WPS433
    doc = await db["runtime_flags"].find_one(
        {"_id": FLAG_ID}, {"_id": 0}, max_time_ms=3000) or {}
    return {**DEFAULTS, **doc}


def counterfactual_return_pct(row: dict, cost_pct: float) -> Optional[float]:
    """Per-signal forward return after estimated costs."""
    outcome = row.get("outcome")
    if outcome == "tp_hit":
        gross = float(row.get("tp_pct") or 5.0)
    elif outcome == "sl_hit":
        gross = -float(row.get("sl_pct") or 3.0)
    elif outcome == "expired":
        gross = float(row.get("end_pct") or 0.0)
    else:
        return None
    return round(gross - cost_pct, 4)


def evaluate_lane(returns: list[float], cfg: dict) -> dict:
    """Pure criteria evaluation over forward returns (pct, after costs)."""
    n = len(returns)
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    expectancy = round(sum(returns) / n, 4) if n else None
    gross_w, gross_l = sum(wins), abs(sum(losses))
    pf = round(gross_w / gross_l, 3) if gross_l > 0 else (
        None if not wins else float("inf"))
    peak = dd = cum = 0.0
    for r in returns:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    share = round(max(wins) / gross_w, 3) if gross_w > 0 else None
    criteria = [
        {"name": "observations", "value": n,
         "threshold": f">= {cfg['min_n']}", "pass": n >= int(cfg["min_n"])},
        {"name": "expectancy_pct_after_costs", "value": expectancy,
         "threshold": "> 0",
         "pass": expectancy is not None and expectancy > 0},
        {"name": "profit_factor", "value": (None if pf == float("inf") else pf),
         "threshold": f"> {cfg['min_profit_factor']}",
         "pass": pf is not None and pf > float(cfg["min_profit_factor"])},
        {"name": "max_drawdown_pct_points", "value": round(dd, 3),
         "threshold": f"<= {cfg['max_drawdown_pct_points']}",
         "pass": dd <= float(cfg["max_drawdown_pct_points"])},
        {"name": "single_trade_dependence", "value": share,
         "threshold": f"<= {cfg['max_single_trade_share']}",
         "pass": share is None or n < 2
         or share <= float(cfg["max_single_trade_share"])},
    ]
    return {"n": n, "criteria": criteria,
            "passed": all(c["pass"] for c in criteria)}


async def gate_status() -> dict:
    from db import db  # noqa: WPS433
    from shared.risk_sizer.missed_entries import COLLECTION  # noqa: WPS433
    cfg = await get_gate_config()
    cut = (datetime.now(timezone.utc)
           - timedelta(days=float(cfg["window_days"]))).isoformat()
    # 2026-08-08 operator decision: funds-blocked BUYs are real strategy
    # signals (cash locked in stuck positions must not stop learning) —
    # they count alongside exit_only blocks, tagged by block_reason.
    _COUNTED = "exit_only_mode|insufficient_balance|no_balance_no_trade"
    rows = await db[COLLECTION].find(
        {"evaluated_at": {"$gte": cut},
         "block_reason": {"$regex": _COUNTED},
         "outcome": {"$in": ["tp_hit", "sl_hit", "expired"]}},
        {"_id": 0, "lane": 1, "outcome": 1, "tp_pct": 1, "sl_pct": 1,
         "end_pct": 1, "blocked_at": 1, "block_reason": 1},
    ).sort("blocked_at", 1).max_time_ms(8000).to_list(2000)
    lanes: dict[str, list[float]] = {"crypto": [], "equity": []}
    tags: dict[str, int] = {}
    for r in rows:
        tag = ("funds_blocked" if "balance" in (r.get("block_reason") or "")
               else "exit_only")
        tags[tag] = tags.get(tag, 0) + 1
        ret = counterfactual_return_pct(r, float(cfg["cost_pct"]))
        if ret is not None:
            lanes.setdefault(r.get("lane") or "crypto", []).append(ret)
    per_lane = {lane: evaluate_lane(rets, cfg)
                for lane, rets in lanes.items()}
    return {
        "config": cfg, "window_start": cut, "per_lane": per_lane,
        "observation_tags": tags,
        "passed": bool(per_lane) and any(v["passed"]
                                         for v in per_lane.values()),
        "passed_lanes": [k for k, v in per_lane.items() if v["passed"]],
        "note": ("forward observations accrue while the system sits in "
                 "exit_only — shadow-filled entries AND funds-blocked "
                 "signals are scored by the missed-entry ledger 4h after "
                 "each block"),
    }
