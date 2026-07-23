"""Expectancy model — realized P&L minus estimated fee + spread drag.

Answers the operator's question: "is ~310 trades/day net-positive
after Kraken taker fees and memecoin spreads?"

Cost model (per closed round trip from `shared_exit_outcomes`):
    turnover = (entry_price + exit_price) * qty      # both sides
    fees     = taker_fee_pct/100 * turnover
    spread   = spread_bps/10000 / 2 * turnover       # half-spread/side
    net      = realized_pnl_usd - fees - spread

Assumptions are lane-scoped knobs stored in
`runtime_flags._id=expectancy_model` (crypto default 0.40%/side —
Kraken Pro spot tier 0 taker; equity default 0 — Webull commission-free).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db import db

FLAG_ID = "expectancy_model"
EXIT_OUTCOMES = "shared_exit_outcomes"

DEFAULTS: dict[str, Any] = {
    "crypto": {"taker_fee_pct": 0.40, "spread_bps": 20.0},
    "equity": {"taker_fee_pct": 0.0, "spread_bps": 5.0},
}
LANE_FIELDS = {"taker_fee_pct", "spread_bps"}


async def get_config() -> dict:
    try:
        doc = await db["runtime_flags"].find_one({"_id": FLAG_ID}) or {}
    except Exception:  # noqa: BLE001
        doc = {}
    out: dict = {}
    for lane in ("equity", "crypto"):
        merged = dict(DEFAULTS[lane])
        stored = doc.get(lane) or {}
        for k in LANE_FIELDS:
            if k in stored and stored[k] is not None:
                merged[k] = float(stored[k])
        out[lane] = merged
    return out


async def set_config(lane: str, fields: dict, updated_by: str) -> dict:
    update = {f"{lane}.{k}": float(v) for k, v in fields.items() if k in LANE_FIELDS}
    update["updated_by"] = updated_by
    update["updated_at"] = datetime.now(timezone.utc).isoformat()
    await db["runtime_flags"].update_one(
        {"_id": FLAG_ID}, {"$set": update}, upsert=True,
    )
    return await get_config()


def row_costs(row: dict, cfg: dict) -> Optional[dict]:
    """Gross/fee/spread/net for one outcome row. None = unpriced row
    (no realized P&L recorded) — excluded from USD aggregates."""
    pnl = row.get("realized_pnl_usd")
    if pnl is None:
        return None
    lane = row.get("lane") or "crypto"
    lane_cfg = cfg.get(lane) or DEFAULTS.get(lane, {})
    entry = float(row.get("entry_price") or 0)
    qty = float(row.get("qty") or 0)
    exit_p = float(row.get("exit_price") or 0) or entry
    turnover = (entry + exit_p) * qty
    fee = float(lane_cfg.get("taker_fee_pct", 0)) / 100.0 * turnover
    spread = float(lane_cfg.get("spread_bps", 0)) / 10_000.0 / 2.0 * turnover
    gross = float(pnl)
    return {
        "gross": gross, "fee": fee, "spread": spread,
        "net": gross - fee - spread,
    }


def _bucket() -> dict:
    return {
        "trades": 0, "gross_pnl_usd": 0.0, "est_fees_usd": 0.0,
        "est_spread_usd": 0.0, "net_pnl_usd": 0.0,
        "wins": 0, "win_sum": 0.0, "loss_sum": 0.0,
    }


def _fold(b: dict, c: dict) -> None:
    b["trades"] += 1
    b["gross_pnl_usd"] += c["gross"]
    b["est_fees_usd"] += c["fee"]
    b["est_spread_usd"] += c["spread"]
    b["net_pnl_usd"] += c["net"]
    if c["net"] > 0:
        b["wins"] += 1
        b["win_sum"] += c["net"]
    else:
        b["loss_sum"] += c["net"]


def _finalize(b: dict) -> dict:
    n = b["trades"]
    losses = n - b["wins"]
    loss_abs = abs(b["loss_sum"])
    return {
        "trades": n,
        "gross_pnl_usd": round(b["gross_pnl_usd"], 4),
        "est_fees_usd": round(b["est_fees_usd"], 4),
        "est_spread_usd": round(b["est_spread_usd"], 4),
        "net_pnl_usd": round(b["net_pnl_usd"], 4),
        "wins": b["wins"],
        "losses": losses,
        "win_rate_pct": round(b["wins"] / n * 100.0, 1) if n else None,
        "expectancy_usd": round(b["net_pnl_usd"] / n, 4) if n else None,
        "avg_win_usd": round(b["win_sum"] / b["wins"], 4) if b["wins"] else None,
        "avg_loss_usd": round(b["loss_sum"] / losses, 4) if losses else None,
        "profit_factor": (
            round(b["win_sum"] / loss_abs, 3) if loss_abs > 1e-9 else None
        ),
    }


async def summary(days: int = 30) -> dict:
    """Aggregate expectancy over the window: overall + per-lane +
    per-brain + per-confluence-mode + daily trend."""
    cfg = await get_config()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    overall = _bucket()
    by_lane: dict[str, dict] = {}
    by_brain: dict[tuple, dict] = {}
    by_confluence: dict[str, dict] = {}
    daily: dict[str, dict] = {}
    unpriced = 0

    async for row in db[EXIT_OUTCOMES].find(
        {"closed_at": {"$gte": since}}, {"_id": 0},
    ):
        c = row_costs(row, cfg)
        if c is None:
            unpriced += 1
            continue
        _fold(overall, c)
        lane = row.get("lane") or "unknown"
        _fold(by_lane.setdefault(lane, _bucket()), c)
        bkey = (row.get("brain") or "unattributed", lane)
        _fold(by_brain.setdefault(bkey, _bucket()), c)
        mode = row.get("confluence_mode") or "unknown"
        _fold(by_confluence.setdefault(mode, _bucket()), c)
        day = str(row.get("closed_at") or "")[:10]
        if day:
            _fold(daily.setdefault(day, _bucket()), c)

    pf = _finalize(overall)
    brains = [
        {"brain": k[0], "lane": k[1], **_finalize(v)}
        for k, v in by_brain.items()
    ]
    brains.sort(key=lambda r: r["net_pnl_usd"], reverse=True)
    return {
        "window_days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": cfg,
        "unpriced_rows": unpriced,
        "overall": pf,
        "by_lane": {k: _finalize(v) for k, v in by_lane.items()},
        "by_brain": brains,
        "by_confluence": {k: _finalize(v) for k, v in by_confluence.items()},
        "daily": [
            {"date": d, **_finalize(v)} for d, v in sorted(daily.items())
        ][-30:],
    }
