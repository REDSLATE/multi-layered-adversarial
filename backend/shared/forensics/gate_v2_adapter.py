"""RISEDUAL wiring for Promotion Gate v2 (operator-supplied module).

Maps Mission Control's data model onto the dependency-free
`promotion_gate_v2` engine:

  observations — missed_entry_outcomes scored rows (gross counterfactual
                 return, ordered by blocked_at)
  fills        — successful broker executions (fee knob by liquidity +
                 measured slippage vs the intended limit price)
  epochs       — runtime_flags `_id=evaluation_epoch_v2`; rows are
                 assigned to the current epoch by timestamp (signal
                 blocked/filled AFTER epoch start), older rows fall
                 into "legacy" so a materially changed execution build
                 is never poisoned by pre-change losses

Config knobs: runtime_flags `_id=promotion_gate_v2` (operator-owned —
thresholds NEVER auto-relax; NEEDS_RECALIBRATION only surfaces them).
"""
from __future__ import annotations

import logging
from dataclasses import fields as dc_fields
from datetime import datetime, timezone
from typing import Optional

from shared.forensics.promotion_gate_v2 import (
    EvaluationObservation,
    ExecutionFill,
    MeasuredCostFeed,
    PromotionConfig,
    PromotionGate,
    begin_evaluation_epoch,
)

logger = logging.getLogger("risedual.gate_v2")

CONFIG_FLAG = "promotion_gate_v2"
EPOCH_FLAG = "evaluation_epoch_v2"
EPOCHS = "evaluation_epochs"

# Per-leg fee knobs (percent). Defaults align with the system's own
# cost assumptions: 2×0.15 = 0.30% assumed taker round trip,
# 2×0.08 = 0.16% maker round trip. Operator-adjustable.
FEE_DEFAULTS = {"maker_leg_fee_pct": 0.08, "taker_leg_fee_pct": 0.15}


async def get_v2_config() -> tuple[PromotionConfig, dict]:
    from db import db  # noqa: WPS433
    doc = await db["runtime_flags"].find_one(
        {"_id": CONFIG_FLAG}, {"_id": 0}, max_time_ms=3000) or {}
    valid = {f.name for f in dc_fields(PromotionConfig)}
    cfg = PromotionConfig(**{k: v for k, v in doc.items() if k in valid})
    fees = {**FEE_DEFAULTS,
            **{k: v for k, v in doc.items() if k in FEE_DEFAULTS}}
    return cfg, fees


async def current_epoch() -> dict:
    """{epoch_id, started_at|None, reason}. No epoch begun yet →
    epoch_id 'default' and every row counts (legacy behavior)."""
    from db import db  # noqa: WPS433
    doc = await db["runtime_flags"].find_one(
        {"_id": EPOCH_FLAG}, {"_id": 0}, max_time_ms=3000) or {}
    if not doc.get("epoch_id"):
        return {"epoch_id": "default", "started_at": None, "reason": None}
    return {"epoch_id": doc["epoch_id"],
            "started_at": doc.get("started_at"),
            "reason": doc.get("reason"),
            "code_revision": doc.get("code_revision")}


async def begin_epoch(reason: str, user_email: str = "operator") -> dict:
    """Begin a new evaluation epoch on a material execution change
    (maker ladder, fee model, broker adapter...). Old observations
    remain in lifetime reference but stop gating readiness."""
    import subprocess  # noqa: WPS433
    from db import db  # noqa: WPS433
    try:
        rev = subprocess.run(
            ["git", "-C", "/app", "log", "-1", "--format=%h"],
            capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        rev = "unknown"
    epoch = begin_evaluation_epoch(code_revision=rev or "unknown",
                                   reason=reason)
    doc = {"epoch_id": epoch.epoch_id,
           "started_at": epoch.started_at.isoformat(),
           "reason": reason, "code_revision": epoch.code_revision,
           "begun_by": user_email}
    await db["runtime_flags"].update_one(
        {"_id": EPOCH_FLAG}, {"$set": doc}, upsert=True)
    await db[EPOCHS].insert_one({**doc, "_id": epoch.epoch_id})
    logger.info("evaluation epoch begun: %s (%s)", epoch.epoch_id, reason)
    return doc


def _epoch_for(ts: Optional[str], epoch: dict) -> str:
    started = epoch.get("started_at")
    if not started:
        return epoch["epoch_id"]  # "default" — everything counts
    return epoch["epoch_id"] if (ts or "") >= started else "legacy"


def _parse_dt(ts) -> datetime:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)


async def load_observations(lane: str, epoch: dict) -> list[EvaluationObservation]:
    from db import db  # noqa: WPS433
    from shared.forensics.promotion_gate import counterfactual_return_pct  # noqa: WPS433
    from shared.risk_sizer.missed_entries import COLLECTION  # noqa: WPS433
    rows = await db[COLLECTION].find(
        {"lane": lane,
         "block_reason": {"$regex":
                          "exit_only_mode|insufficient_balance|no_balance_no_trade"},
         "outcome": {"$in": ["tp_hit", "sl_hit", "expired"]}},
        {"_id": 1, "outcome": 1, "tp_pct": 1, "sl_pct": 1, "end_pct": 1,
         "blocked_at": 1, "symbol": 1, "stack": 1},
    ).sort("blocked_at", 1).max_time_ms(10000).to_list(8000)
    out = []
    for r in rows:
        gross = counterfactual_return_pct(r, 0.0)
        if gross is None:
            continue
        ts = str(r.get("blocked_at") or "")
        out.append(EvaluationObservation(
            observation_id=str(r["_id"]),
            resolved_at=_parse_dt(ts),
            gross_return_pct=float(gross),
            symbol=r.get("symbol") or "",
            strategy=r.get("stack") or "shadow",
            epoch_id=_epoch_for(ts, epoch),
        ))
    return out


async def load_fills(lane: str, epoch: dict, fees: dict) -> list[ExecutionFill]:
    """Measured-cost feed. PRIMARY source: `broker_fills_ledger`
    (broker-confirmed fills with ACTUAL exchange fees — 2026-06
    reconciliation directive). Fallbacks: execution_fill_costs legs,
    then legacy executions estimate."""
    from db import db  # noqa: WPS433
    ledger = await db["broker_fills_ledger"].find(
        {"lane": lane, "price": {"$gt": 0}, "qty": {"$gt": 0}},
    ).sort("ts", 1).max_time_ms(8000).to_list(4000)
    if ledger:
        out = []
        for r in ledger:
            ts = str(r.get("ts") or "")
            cost = float(r.get("cost_usd") or 0) or (
                float(r["price"]) * float(r["qty"]))
            fee_usd = r.get("fee_usd")
            if fee_usd is not None and cost > 0:
                fee_pct = float(fee_usd) / cost * 100.0
            else:
                fee_pct = float(fees["maker_leg_fee_pct"] if r.get("maker")
                                else fees["taker_leg_fee_pct"])
            liq = ("maker" if r.get("maker")
                   else "taker" if r.get("maker") is False else "unknown")
            out.append(ExecutionFill(
                fill_id=str(r["_id"]),
                trade_id=(r.get("link") or {}).get("intent_id")
                or str(r["_id"]),
                timestamp=_parse_dt(ts),
                side=(r.get("side") or "BUY").upper(),
                fill_price=float(r["price"]),
                quantity=float(r["qty"]),
                fee_pct=round(fee_pct, 6),
                reference_price=(r.get("signal_price")
                                 or r.get("submitted_limit_price")),
                liquidity=liq,
                epoch_id=_epoch_for(ts, epoch),
            ))
        return out
    return await _load_fills_captured(lane, epoch, fees)


async def _load_fills_captured(lane: str, epoch: dict,
                               fees: dict) -> list[ExecutionFill]:
    from db import db  # noqa: WPS433
    rows = await db["execution_fill_costs"].find(
        {"lane": lane, "status": "resolved", "fill_price": {"$gt": 0}},
    ).sort("ts", 1).max_time_ms(8000).to_list(4000)
    if rows:
        out = []
        for r in rows:
            ts = str(r.get("ts") or "")
            fee_pct = r.get("fee_pct")
            if fee_pct is None:
                fee_pct = float(
                    fees["maker_leg_fee_pct"]
                    if r.get("liquidity") == "maker"
                    else fees["taker_leg_fee_pct"])
            out.append(ExecutionFill(
                fill_id=str(r["_id"]),
                trade_id=r.get("intent_id") or str(r["_id"]),
                timestamp=_parse_dt(ts),
                side=(r.get("side") or "BUY").upper(),
                fill_price=float(r["fill_price"]),
                quantity=float(r.get("qty_filled") or 1.0),
                fee_pct=float(fee_pct),
                reference_price=(r.get("signal_price")
                                 or r.get("submitted_limit_price")),
                liquidity=r.get("liquidity") or "unknown",
                epoch_id=_epoch_for(ts, epoch),
            ))
        return out
    return await _load_fills_legacy(lane, epoch, fees)


async def _load_fills_legacy(lane: str, epoch: dict,
                             fees: dict) -> list[ExecutionFill]:
    from db import db  # noqa: WPS433
    rows = await db["executions"].find(
        {"ok": True, "lane": lane,
         "broker_response.filled_avg_price": {"$gt": 0}},
        {"_id": 1, "intent_id": 1, "ts": 1, "action": 1,
         "broker_response.filled_avg_price": 1,
         "broker_response.limit_price": 1,
         "broker_response.order_style": 1,
         "broker_response.volume_base": 1},
    ).sort("ts", 1).max_time_ms(10000).to_list(4000)
    out = []
    for r in rows:
        br = r.get("broker_response") or {}
        style = str(br.get("order_style") or "")
        maker = style in ("post_only_limit", "recovery_ladder")
        fee = float(fees["maker_leg_fee_pct"] if maker
                    else fees["taker_leg_fee_pct"])
        ts = str(r.get("ts") or "")
        out.append(ExecutionFill(
            fill_id=str(r["_id"]),
            trade_id=r.get("intent_id") or str(r["_id"]),
            timestamp=_parse_dt(ts),
            side=(r.get("action") or "BUY").upper(),
            fill_price=float(br["filled_avg_price"]),
            quantity=float(br.get("volume_base") or 1.0),
            fee_pct=fee,
            reference_price=(float(br["limit_price"])
                             if br.get("limit_price") else None),
            liquidity="maker" if maker else "taker",
            epoch_id=_epoch_for(ts, epoch),
        ))
    return out


class PairedCostFeed(MeasuredCostFeed):
    """Exact realized round-trip cost from FIFO-paired entry/exit legs
    (2026-06 directive) — replaces the 2×avg-leg estimate once enough
    pairs exist. Assumed cost stays authoritative below the fill
    minimum, exactly as the module designed."""

    MIN_PAIRS = 5

    def __init__(self, assumed: float, min_fills: int, pairs: list[dict]):
        super().__init__(assumed, min_fills)
        self.pairs = [p for p in pairs
                      if p.get("round_trip_cost_pct") is not None]

    def estimate(self, fills, epoch_id):
        base = super().estimate(fills, epoch_id)
        if base.source != "measured" or len(self.pairs) < self.MIN_PAIRS:
            return base
        from statistics import mean as _mean  # noqa: WPS433
        from shared.forensics.promotion_gate_v2 import CostEstimate  # noqa: WPS433
        return CostEstimate(
            source="measured",
            round_trip_cost_pct=max(0.0, _mean(
                p["round_trip_cost_pct"] for p in self.pairs)),
            eligible_fill_count=base.eligible_fill_count,
            maker_fill_count=base.maker_fill_count,
            taker_fill_count=base.taker_fill_count,
            avg_leg_cost_pct=base.avg_leg_cost_pct,
        )


async def _epoch_pairs(lane: str, epoch: dict,
                       epoch_id: str) -> list[dict]:
    from db import db  # noqa: WPS433
    # Authoritative pairs: reconciled trade_outcomes (broker truth).
    outs = await db["trade_outcomes"].find(
        {"lane": lane, "measured_cost_eligible": True},
        {"round_trip_cost_pct": 1, "net_return_pct": 1, "entry_ts": 1},
    ).sort("entry_ts", 1).max_time_ms(8000).to_list(4000)
    outs = [o for o in outs
            if _epoch_for(str(o.get("entry_ts") or ""), epoch) == epoch_id]
    if outs:
        return outs
    from shared.execution_costs import pair_round_trips  # noqa: WPS433
    rows = await db["execution_fill_costs"].find(
        {"lane": lane, "status": "resolved"},
    ).sort("ts", 1).max_time_ms(8000).to_list(4000)
    rows = [r for r in rows
            if _epoch_for(str(r.get("ts") or ""), epoch) == epoch_id]
    return pair_round_trips(rows)


async def evaluate_lane(lane: str) -> dict:
    cfg, fees = await get_v2_config()
    epoch = await current_epoch()
    obs = await load_observations(lane, epoch)
    fills = await load_fills(lane, epoch, fees)
    pairs = await _epoch_pairs(lane, epoch, epoch["epoch_id"])
    decision = PromotionGate(
        cfg, cost_feed=PairedCostFeed(
            cfg.assumed_round_trip_cost_pct, cfg.min_measured_fills, pairs),
    ).evaluate(observations=obs, fills=fills, epoch_id=epoch["epoch_id"])
    d = decision.to_dict()
    d["paired_round_trips"] = len(pairs)
    return d


async def v2_status() -> dict:
    cfg, fees = await get_v2_config()
    epoch = await current_epoch()
    per_lane = {lane: await evaluate_lane(lane)
                for lane in ("crypto", "equity")}
    passed = [k for k, v in per_lane.items() if v["state"] == "PASS"]
    return {
        "epoch": epoch,
        "config": {**{f.name: getattr(cfg, f.name)
                      for f in dc_fields(PromotionConfig)}, **fees},
        "per_lane": per_lane,
        "passed_lanes": passed,
        "passed": bool(passed),
    }


def _bucket_stats(grosses: list[float], cost_pct: float) -> dict:
    """Comparison-view stats for one epoch bucket. Diagnostic only."""
    from shared.forensics.promotion_gate_v2 import (  # noqa: WPS433
        _drawdown_per_100, _max_drawdown_pct, _profit_factor,
    )
    n = len(grosses)
    if n == 0:
        return {"n": 0}
    nets = [g - cost_pct for g in grosses]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    dd = _max_drawdown_pct(nets)
    pf = _profit_factor(nets)
    return {
        "n": n,
        "gross_expectancy_pct": round(sum(grosses) / n, 4),
        "net_expectancy_pct": round(sum(nets) / n, 4),
        "profit_factor": (None if pf == float("inf") else round(pf, 3)),
        "win_rate": round(len(wins) / n, 3),
        "avg_winner_pct": round(sum(wins) / len(wins), 4) if wins else None,
        "avg_loser_pct": round(sum(losses) / len(losses), 4) if losses else None,
        "observation_drawdown_per_100": round(_drawdown_per_100(dd, n), 3),
    }


async def epoch_comparison(lane: str) -> dict:
    """Active epoch vs legacy, side by side. NEVER merged into one
    promotion score — diagnostic/comparative only (2026-06 directive)."""
    from db import db  # noqa: WPS433
    cfg, fees = await get_v2_config()
    epoch = await current_epoch()
    obs = await load_observations(lane, epoch)
    fills = await load_fills(lane, epoch, fees)
    out = {"lane": lane, "epoch": epoch, "buckets": {}}
    bucket_ids = ([epoch["epoch_id"], "legacy"]
                  if epoch.get("started_at") else [epoch["epoch_id"]])
    for eid in bucket_ids:
        b_obs = [o.gross_return_pct for o in obs if o.epoch_id == eid]
        b_fills = [f for f in fills if f.epoch_id == eid]
        pairs = await _epoch_pairs(lane, epoch, eid)
        cost = PairedCostFeed(
            cfg.assumed_round_trip_cost_pct, cfg.min_measured_fills, pairs,
        ).estimate(b_fills, eid)
        maker = sum(1 for f in b_fills if f.liquidity == "maker")
        taker = sum(1 for f in b_fills if f.liquidity == "taker")
        # realized account curve from ACTUAL round trips (incl manual
        # orphan resolutions) — separate from the observation metric.
        pairs_all = await db["trade_outcomes"].find(
            {"lane": lane},
            {"net_return_pct": 1, "entry_ts": 1, "exit_ts": 1},
        ).sort("exit_ts", 1).max_time_ms(8000).to_list(4000)
        real_nets = [p["net_return_pct"] for p in pairs_all
                     if p.get("net_return_pct") is not None
                     and _epoch_for(str(p.get("entry_ts")
                                        or p.get("exit_ts") or ""),
                                    epoch) == eid]
        if not real_nets:
            real_nets = [p["net_return_pct"] for p in pairs
                         if p.get("net_return_pct") is not None]
        from shared.forensics.promotion_gate_v2 import _max_drawdown_pct  # noqa: WPS433
        realized_dd = (_max_drawdown_pct(real_nets) if real_nets else None)
        out["buckets"][eid] = {
            **_bucket_stats(b_obs, cost.round_trip_cost_pct),
            "cost": cost.to_dict(),
            "fills": len(b_fills),
            "maker_taker_ratio": (f"{maker}/{taker}"),
            "paired_round_trips": len(pairs),
            "realized_trades": len(real_nets),
            "realized_total_return_pct": (round(sum(real_nets), 3)
                                          if real_nets else None),
            "realized_account_max_drawdown_pct": (round(realized_dd, 3)
                                                  if realized_dd is not None
                                                  else None),
        }
    out["note"] = ("epochs are never merged into one promotion score — "
                   "observation-curve drawdown and realized account "
                   "drawdown are separate metrics by design")
    return out
