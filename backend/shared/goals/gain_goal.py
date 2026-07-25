"""Gain Goal — operator profit objectives per lane (2026-07-25 spec).

Doctrine: the Gain Goal MEASURES whether the system is accomplishing
the operator's objective. It never manufactures the outcome — pace is
informational; it cannot lower doctrine thresholds, raise frequency
or size, override the Seat, or suppress valid trades. The only two
levers are RISK-REDUCING: the drawdown hard stop (binding rule,
blocks NEW ENTRIES for the lane until operator ack or window reset)
and the optional ahead-of-pace throttle (caps new-entry notional).

Data source = the Expectancy Panel's resolved outcomes
(`shared_exit_outcomes`, net of fees + spread). No unrealized P&L,
no marks, no deposits/transfers, no advisory/witness rows.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from db import db
from namespaces import CAPITAL_LEDGER
from shared.expectancy import EXIT_OUTCOMES, get_config as get_expectancy_config, row_costs

FLAG_ID = "gain_goals"
STATE_FLAG_ID = "gain_goal_state"
LANES = ("equity", "crypto")

STATUSES = (
    "NO_GOAL", "INSUFFICIENT_SAMPLE", "ON_PACE", "AHEAD_OF_PACE",
    "BEHIND_PACE", "GOAL_REACHED", "DRAWDOWN_WARNING",
    "DRAWDOWN_BREACHED", "WINDOW_COMPLETE",
)

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "defaults": {
        "primary_metric": "net_realized_pnl_usd",
        "secondary_metric": "net_return_on_deployed_capital_pct",
        "window_type": "calendar_month",   # calendar_week | rolling_days | custom
        "rolling_days": 30,
        "custom_start": None,              # ISO date, custom windows only
        "custom_end": None,
        "pace_tracking": "linear_session_adjusted",
        "behind_pace_action": "alert_only",
        "ahead_pace_action": "none",
        "drawdown_action": "pause_new_entries",
        "and_condition": False,
        "pace_tolerance_pct_of_target": 5.0,
        "drawdown_warning_pct_of_limit": 80.0,
    },
    "equity": {
        "enabled": True,
        "session_scope": "RTH",
        "target_net_pnl_usd": None,
        "target_return_pct": None,
        "maximum_window_drawdown_usd": None,
        "maximum_window_drawdown_pct": None,
        "minimum_resolved_trades": 20,
        # per-lane overrides of the defaults block (None → inherit)
        "window_type": None,
        "rolling_days": None,
        "custom_start": None,
        "custom_end": None,
        "primary_metric": None,
        "and_condition": None,
    },
    "crypto": {
        "enabled": True,
        "session_scope": "continuous",
        "target_net_pnl_usd": None,
        "target_return_pct": None,
        "maximum_window_drawdown_usd": None,
        "maximum_window_drawdown_pct": None,
        "minimum_resolved_trades": 20,
        "window_type": None,
        "rolling_days": None,
        "custom_start": None,
        "custom_end": None,
        "primary_metric": None,
        "and_condition": None,
    },
    # Built now per operator directive; may ONLY reduce risk. Inert
    # until a lane has a target set.
    "ahead_of_pace_throttle": {
        "enabled": True,
        "activation_pct_of_goal": 100.0,
        "notional_multiplier": 0.50,
    },
}

_LANE_OVERRIDABLE = (
    "window_type", "rolling_days", "custom_start", "custom_end",
    "primary_metric", "and_condition",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _merge(stored: dict) -> dict:
    """Deep-merge stored config over DEFAULTS (None values in stored
    are meaningful for targets — kept)."""
    out: dict = {"enabled": bool(stored.get("enabled", DEFAULTS["enabled"]))}
    for section in ("defaults", "equity", "crypto", "ahead_of_pace_throttle"):
        merged = dict(DEFAULTS[section])
        for k, v in (stored.get(section) or {}).items():
            if k in merged:
                merged[k] = v
        out[section] = merged
    return out


async def get_goal_config() -> dict:
    try:
        stored = await db["runtime_flags"].find_one({"_id": FLAG_ID}, {"_id": 0}) or {}
    except Exception:  # noqa: BLE001
        stored = {}
    return _merge(stored)


def _lane_setting(cfg: dict, lane: str, key: str):
    lane_cfg = cfg.get(lane) or {}
    if key in _LANE_OVERRIDABLE and lane_cfg.get(key) is not None:
        return lane_cfg[key]
    if key in lane_cfg and key not in _LANE_OVERRIDABLE:
        return lane_cfg.get(key)
    return (cfg.get("defaults") or {}).get(key)


# ─────────────────────── window + pace math ─────────────────────────

def window_bounds(
    window_type: str,
    now: Optional[datetime] = None,
    rolling_days: int = 30,
    custom_start: Optional[str] = None,
    custom_end: Optional[str] = None,
) -> tuple[datetime, datetime, str]:
    """Return (start, end, label) in UTC."""
    now = now or _now()
    if window_type == "calendar_week":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=7), f"WEEK {start.strftime('%b %d')}"
    if window_type == "rolling_days":
        days = max(1, int(rolling_days or 30))
        return now - timedelta(days=days), now, f"ROLLING {days}D"
    if window_type == "custom" and custom_start and custom_end:
        s = datetime.fromisoformat(str(custom_start)).replace(tzinfo=timezone.utc) \
            if "T" not in str(custom_start) else datetime.fromisoformat(str(custom_start))
        e = datetime.fromisoformat(str(custom_end)).replace(tzinfo=timezone.utc) \
            if "T" not in str(custom_end) else datetime.fromisoformat(str(custom_end))
        if s.tzinfo is None:
            s = s.replace(tzinfo=timezone.utc)
        if e.tzinfo is None:
            e = e.replace(tzinfo=timezone.utc)
        return s, e, f"CUSTOM → {e.strftime('%b %d')}"
    # default: calendar_month
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last = calendar.monthrange(now.year, now.month)[1]
    end = start + timedelta(days=last)
    return start, end, now.strftime("%B").upper()


def _rth_session_progress(start: datetime, end: datetime, now: datetime) -> dict:
    """Equity pace uses completed eligible RTH sessions, not calendar
    days. Today's session counts fractionally (09:30-16:00 ET)."""
    from shared.market_hours import _is_business_day, _to_et  # noqa: WPS433
    total = 0
    completed = 0.0
    d = start.date()
    today_et = _to_et(now).date()
    while d < end.date():
        if _is_business_day(d):
            total += 1
            if d < today_et:
                completed += 1.0
            elif d == today_et:
                et = _to_et(now)
                open_min = 9 * 60 + 30
                close_min = 16 * 60
                mins = et.hour * 60 + et.minute
                frac = (mins - open_min) / (close_min - open_min)
                completed += min(1.0, max(0.0, frac))
        d += timedelta(days=1)
    frac_total = (completed / total) if total else 0.0
    return {
        "mode": "rth_sessions",
        "completed_sessions": round(completed, 2),
        "total_sessions": total,
        "fraction": min(1.0, max(0.0, frac_total)),
    }


def _elapsed_progress(start: datetime, end: datetime, now: datetime) -> dict:
    span = (end - start).total_seconds()
    frac = ((now - start).total_seconds() / span) if span > 0 else 1.0
    return {
        "mode": "elapsed_time",
        "elapsed_hours": round(max(0.0, (now - start).total_seconds()) / 3600, 1),
        "total_hours": round(span / 3600, 1),
        "fraction": min(1.0, max(0.0, frac)),
    }


# ───────────────────────── evaluation ───────────────────────────────

async def _deployed_capital(lane: str) -> Optional[float]:
    try:
        doc = await db[CAPITAL_LEDGER].find_one({"_id": f"{lane}_cap"}, {"total": 1})
        total = (doc or {}).get("total")
        return float(total) if total else None
    except Exception:  # noqa: BLE001
        return None


def _brain_bucket() -> dict:
    return {"trades": 0, "net": 0.0, "wins": 0, "win_sum": 0.0,
            "loss_sum": 0.0, "cum": 0.0, "peak": 0.0, "max_dd": 0.0}


async def evaluate_lane(lane: str, cfg: dict, now: Optional[datetime] = None) -> dict:
    """Evaluate one lane's goal window. Pure measurement — the caller
    (worker) owns the breach latch and any risk-reducing publication."""
    now = now or _now()
    lane_cfg = cfg.get(lane) or {}
    window_type = _lane_setting(cfg, lane, "window_type") or "calendar_month"
    start, end, label = window_bounds(
        window_type, now,
        rolling_days=_lane_setting(cfg, lane, "rolling_days") or 30,
        custom_start=_lane_setting(cfg, lane, "custom_start"),
        custom_end=_lane_setting(cfg, lane, "custom_end"),
    )

    exp_cfg = await get_expectancy_config()
    trades = 0
    net = 0.0
    gross = 0.0
    fees = 0.0
    spread = 0.0
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    excluded = 0
    brains: dict[str, dict] = {}

    cursor = db[EXIT_OUTCOMES].find(
        {"closed_at": {"$gte": start.isoformat(), "$lt": end.isoformat()},
         "lane": lane},
        {"_id": 0},
    ).sort("closed_at", 1)
    async for row in cursor:
        # Doctrine exclusions: unresolved witness outcomes, advisory /
        # shadow trades never count toward the goal.
        if row.get("witness") or row.get("advisory") or row.get("shadow"):
            excluded += 1
            continue
        c = row_costs(row, exp_cfg)
        if c is None:
            excluded += 1
            continue
        trades += 1
        net += c["net"]
        gross += c["gross"]
        fees += c["fee"]
        spread += c["spread"]
        cum += c["net"]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        b = brains.setdefault(row.get("brain") or "unattributed", _brain_bucket())
        b["trades"] += 1
        b["net"] += c["net"]
        b["cum"] += c["net"]
        b["peak"] = max(b["peak"], b["cum"])
        b["max_dd"] = max(b["max_dd"], b["peak"] - b["cum"])
        if c["net"] > 0:
            b["wins"] += 1
            b["win_sum"] += c["net"]
        else:
            b["loss_sum"] += c["net"]

    current_dd = peak - cum

    # progress
    session_scope = lane_cfg.get("session_scope") or ("RTH" if lane == "equity" else "continuous")
    if window_type == "rolling_days":
        progress = {"mode": "rolling", "fraction": 1.0}
    elif session_scope == "RTH":
        progress = _rth_session_progress(start, end, now)
    else:
        progress = _elapsed_progress(start, end, now)
    frac = float(progress["fraction"])

    # targets → USD-equivalent primary target
    deployed = await _deployed_capital(lane)
    target_usd = lane_cfg.get("target_net_pnl_usd")
    target_pct = lane_cfg.get("target_return_pct")
    primary = _lane_setting(cfg, lane, "primary_metric") or "net_realized_pnl_usd"
    and_condition = bool(_lane_setting(cfg, lane, "and_condition"))
    effective_target: Optional[float] = None
    if primary == "net_return_on_deployed_capital_pct" and target_pct is not None and deployed:
        effective_target = float(target_pct) / 100.0 * deployed
    elif target_usd is not None:
        effective_target = float(target_usd)
    elif target_pct is not None and deployed:
        effective_target = float(target_pct) / 100.0 * deployed

    return_pct = (net / deployed * 100.0) if deployed else None

    # pace
    expected_pnl = pace_variance = goal_pct = projected = None
    if effective_target:
        expected_pnl = effective_target * frac
        pace_variance = net - expected_pnl
        goal_pct = net / effective_target * 100.0
        if frac >= 0.05:
            projected = net / frac

    # drawdown limits
    dd_limit_usd = lane_cfg.get("maximum_window_drawdown_usd")
    dd_limit_pct = lane_cfg.get("maximum_window_drawdown_pct")
    effective_dd_limit: Optional[float] = None
    if dd_limit_usd is not None:
        effective_dd_limit = float(dd_limit_usd)
    if dd_limit_pct is not None and deployed:
        pct_usd = float(dd_limit_pct) / 100.0 * deployed
        effective_dd_limit = min(effective_dd_limit, pct_usd) \
            if effective_dd_limit is not None else pct_usd

    warn_pct = float(_lane_setting(cfg, lane, "drawdown_warning_pct_of_limit") or 80.0)
    dd_breached = bool(effective_dd_limit is not None and max_dd >= effective_dd_limit)
    dd_warning = bool(
        not dd_breached and effective_dd_limit is not None
        and max_dd >= effective_dd_limit * warn_pct / 100.0
    )

    # goal reached (AND condition requires both configured metrics)
    goal_reached = False
    if effective_target is not None:
        if and_condition and target_usd is not None and target_pct is not None:
            goal_reached = (
                net >= float(target_usd)
                and deployed is not None
                and (return_pct or 0.0) >= float(target_pct)
            )
        else:
            goal_reached = net >= effective_target

    min_trades = int(lane_cfg.get("minimum_resolved_trades") or 0)
    sample_ok = trades >= min_trades

    # status precedence (spec): binding rules first, then completion,
    # then sample, then pace.
    tol_pct = float(_lane_setting(cfg, lane, "pace_tolerance_pct_of_target") or 5.0)
    if not lane_cfg.get("enabled", True) or not cfg.get("enabled", True):
        status = "NO_GOAL"
    elif dd_breached:
        status = "DRAWDOWN_BREACHED"
    elif dd_warning:
        status = "DRAWDOWN_WARNING"
    elif effective_target is None:
        status = "NO_GOAL"
    elif now >= end and window_type == "custom":
        status = "WINDOW_COMPLETE"
    elif goal_reached:
        status = "GOAL_REACHED"
    elif not sample_ok:
        status = "INSUFFICIENT_SAMPLE"
    else:
        tol = abs(effective_target) * tol_pct / 100.0
        if pace_variance is not None and pace_variance > tol:
            status = "AHEAD_OF_PACE"
        elif pace_variance is not None and pace_variance < -tol:
            status = "BEHIND_PACE"
        else:
            status = "ON_PACE"

    brain_rows = []
    for name, b in sorted(brains.items(), key=lambda kv: kv[1]["net"], reverse=True):
        losses = b["trades"] - b["wins"]
        loss_abs = abs(b["loss_sum"])
        brain_rows.append({
            "brain": name,
            "trades": b["trades"],
            "net_pnl_usd": round(b["net"], 4),
            "win_rate_pct": round(b["wins"] / b["trades"] * 100.0, 1) if b["trades"] else None,
            "profit_factor": round(b["win_sum"] / loss_abs, 3) if loss_abs > 1e-9 else None,
            "expectancy_usd": round(b["net"] / b["trades"], 4) if b["trades"] else None,
            "max_drawdown_usd": round(b["max_dd"], 4),
        })

    r2 = lambda v: None if v is None else round(v, 2)  # noqa: E731
    return {
        "lane": lane,
        "status": status,
        "label": label,
        "window_type": window_type,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "session_scope": session_scope,
        "primary_metric": primary,
        "and_condition": and_condition,
        "target_net_pnl_usd": target_usd,
        "target_return_pct": target_pct,
        "effective_target_usd": r2(effective_target),
        "net_realized_pnl_usd": r2(net),
        "gross_pnl_usd": r2(gross),
        "est_fees_usd": r2(fees),
        "est_spread_usd": r2(spread),
        "return_on_deployed_pct": r2(return_pct),
        "deployed_capital_usd": r2(deployed),
        "goal_progress_pct": r2(goal_pct),
        "time_progress": progress,
        "expected_pnl_usd": r2(expected_pnl),
        "pace_variance_usd": r2(pace_variance),
        "projected_end_usd": r2(projected),
        "current_drawdown_usd": r2(current_dd),
        "max_window_drawdown_usd": r2(max_dd),
        "drawdown_limit_usd": r2(effective_dd_limit),
        "drawdown_breached": dd_breached,
        "resolved_trades": trades,
        "minimum_resolved_trades": min_trades,
        "sample_sufficient": sample_ok,
        "excluded_rows": excluded,
        "brain_attribution": brain_rows,
        "evaluated_at": now.isoformat(),
    }


async def evaluate_all(now: Optional[datetime] = None) -> dict:
    cfg = await get_goal_config()
    lanes = {lane: await evaluate_lane(lane, cfg, now) for lane in LANES}
    total_net = sum(v["net_realized_pnl_usd"] or 0.0 for v in lanes.values())
    total_trades = sum(v["resolved_trades"] for v in lanes.values())
    return {
        "config": cfg,
        "lanes": lanes,
        # Read-only rollup — the account has no goal of its own.
        "global_rollup": {
            "net_realized_pnl_usd": round(total_net, 2),
            "resolved_trades": total_trades,
            "read_only": True,
        },
        "evaluated_at": (now or _now()).isoformat(),
    }
