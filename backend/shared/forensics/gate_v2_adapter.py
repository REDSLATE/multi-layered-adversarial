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
    """Real broker fills → measured-cost feed. Fee per leg comes from
    the liquidity knob (maker/taker); slippage is measured fill vs the
    intended limit price when both are present."""
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


async def evaluate_lane(lane: str) -> dict:
    cfg, fees = await get_v2_config()
    epoch = await current_epoch()
    obs = await load_observations(lane, epoch)
    fills = await load_fills(lane, epoch, fees)
    decision = PromotionGate(cfg).evaluate(
        observations=obs, fills=fills, epoch_id=epoch["epoch_id"])
    return decision.to_dict()


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
