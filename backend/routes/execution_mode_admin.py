"""Entry-mode + promotion-gate admin (2026-08-05 operator directive).

Mode changes to canary/live are REFUSED while the promotion gate is
red unless the operator sends override=true — deployment is earned on
forward-recorded expectancy, not on passing tests.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import get_current_user
from db import db

router = APIRouter(prefix="/admin/entry-mode", tags=["entry-mode"])


@router.get("")
async def entry_mode_get(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.execution_mode import (  # noqa: WPS433
        SHADOW_FILLS, get_entry_mode_config,
    )
    shadows = await db[SHADOW_FILLS].find({}, {"_id": 0}).sort(
        "ts", -1).max_time_ms(5000).to_list(10)
    n_shadow = await db[SHADOW_FILLS].count_documents({}, maxTimeMS=5000)
    return {"ok": True, "config": await get_entry_mode_config(),
            "shadow_fills_total": n_shadow, "recent_shadow_fills": shadows}


class ModeBody(BaseModel):
    mode: Optional[Literal["live", "exit_only", "canary"]] = None
    canary_max_trades_per_day: Optional[int] = Field(
        default=None, ge=1, le=50)
    override: bool = False


@router.post("")
async def entry_mode_update(
    body: ModeBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    from shared.execution_mode import FLAG_ID, get_entry_mode_config  # noqa: WPS433
    changes = {k: v for k, v in body.model_dump().items()
               if v is not None and k != "override"}
    if body.mode in ("live", "canary") and not body.override:
        from shared.forensics.promotion_gate import gate_status  # noqa: WPS433
        gate = await gate_status()
        if not gate["passed"]:
            raise HTTPException(409, {
                "error": "promotion_gate_not_met",
                "detail": ("automated entries return only after "
                           "forward-recorded expectancy is positive; "
                           "send override=true to force (audited)"),
                "gate": {k: gate[k] for k in ("per_lane", "passed_lanes")},
            })
    if changes:
        changes["updated_at"] = datetime.now(timezone.utc).isoformat()
        changes["updated_by"] = user.get("email") or "operator"
        if body.override:
            changes["override_used"] = True
        await db["runtime_flags"].update_one(
            {"_id": FLAG_ID}, {"$set": changes}, upsert=True)
    return {"ok": True, "config": await get_entry_mode_config()}


@router.get("/promotion-gate")
async def promotion_gate_get(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.forensics.promotion_gate import gate_status  # noqa: WPS433
    return {"ok": True, **await gate_status()}


@router.get("/edge-slicer")
async def edge_slicer(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Edge Slicer + Cost Autopsy (2026-08-08 operator choice C).
    Mines the scored shadow dataset for slices with positive after-cost
    edge (symbol / hour / weekday / tag / confidence) and splits
    expectancy into gross vs costs so 'signals lose' can be separated
    from 'fees eat the edge'."""
    from datetime import timedelta  # noqa: WPS433
    from shared.forensics.promotion_gate import (  # noqa: WPS433
        counterfactual_return_pct, get_gate_config,
    )
    from shared.risk_sizer.missed_entries import COLLECTION  # noqa: WPS433

    cfg = await get_gate_config()
    cost = float(cfg["cost_pct"])
    cut = (datetime.now(timezone.utc)
           - timedelta(days=float(cfg["window_days"]))).isoformat()
    rows = await db[COLLECTION].find(
        {"evaluated_at": {"$gte": cut},
         "block_reason": {"$regex":
                          "exit_only_mode|insufficient_balance|no_balance_no_trade"},
         "outcome": {"$in": ["tp_hit", "sl_hit", "expired"]}},
        {"_id": 0, "lane": 1, "outcome": 1, "tp_pct": 1, "sl_pct": 1,
         "end_pct": 1, "blocked_at": 1, "block_reason": 1, "symbol": 1,
         "confidence": 1},
    ).max_time_ms(10000).to_list(5000)

    scored = []
    for r in rows:
        gross = counterfactual_return_pct(r, 0.0)
        if gross is None:
            continue
        r["_gross"] = gross
        r["_net"] = round(gross - cost, 4)
        scored.append(r)

    def _stats(subset):
        n = len(subset)
        if not n:
            return None
        nets = [r["_net"] for r in subset]
        wins = [x for x in nets if x > 0]
        losses = [x for x in nets if x < 0]
        gl = abs(sum(losses))
        return {
            "n": n,
            "expectancy_net": round(sum(nets) / n, 4),
            "expectancy_gross": round(sum(r["_gross"] for r in subset) / n, 4),
            "win_rate": round(len(wins) / n, 3),
            "profit_factor": round(sum(wins) / gl, 3) if gl > 0 else None,
        }

    def _slice(key_fn, min_n=30):
        buckets: dict[str, list] = {}
        for r in scored:
            k = key_fn(r)
            if k is not None:
                buckets.setdefault(str(k), []).append(r)
        out = {k: _stats(v) for k, v in buckets.items() if len(v) >= min_n}
        return dict(sorted(out.items(),
                           key=lambda kv: kv[1]["expectancy_net"],
                           reverse=True))

    def _hour_bucket(r):
        ts = str(r.get("blocked_at") or "")
        return f"{int(ts[11:13]) // 4 * 4:02d}-{int(ts[11:13]) // 4 * 4 + 4:02d} UTC" if len(ts) > 12 else None

    def _weekday(r):
        try:
            from datetime import datetime as _dt  # noqa: WPS433
            return _dt.fromisoformat(
                str(r["blocked_at"]).replace("Z", "+00:00")).strftime("%a")
        except Exception:  # noqa: BLE001
            return None

    def _conf_bucket(r):
        c = r.get("confidence")
        if c is None:
            return None
        c = float(c)
        return "conf ≥0.8" if c >= 0.8 else "conf 0.6-0.8" if c >= 0.6 else "conf <0.6"

    def _tag(r):
        return ("funds_blocked" if "balance" in (r.get("block_reason") or "")
                else "exit_only")

    slices = {
        "by_lane": _slice(lambda r: r.get("lane")),
        "by_hour_utc": _slice(_hour_bucket),
        "by_weekday": _slice(_weekday),
        "by_tag": _slice(_tag),
        "by_confidence": _slice(_conf_bucket),
        "by_symbol": _slice(lambda r: r.get("symbol"), min_n=20),
    }
    positive = []
    for dim, buckets in slices.items():
        for name, s in buckets.items():
            if s["expectancy_net"] > 0:
                positive.append({"dimension": dim, "slice": name, **s})
    positive.sort(key=lambda s: s["expectancy_net"], reverse=True)

    overall = _stats(scored) or {}
    gross_e = overall.get("expectancy_gross")
    net_e = overall.get("expectancy_net")
    if gross_e is None:
        autopsy_verdict = "no scored observations yet"
    elif gross_e > 0 and (net_e or 0) <= 0:
        autopsy_verdict = (f"COSTS EAT THE EDGE: gross {gross_e:+.3f}% is "
                           f"positive but {cost:.2f}% assumed costs flip it to "
                           f"{net_e:+.3f}% — cost engineering (maker/limit "
                           "orders, larger min notional) can rescue this")
    elif gross_e <= 0:
        autopsy_verdict = (f"SIGNALS LOSE GROSS: {gross_e:+.3f}% before any "
                           "costs — the entry logic itself needs work; cost "
                           "cuts won't save it")
    else:
        autopsy_verdict = f"POSITIVE NET EDGE: {net_e:+.3f}% after costs"

    return {
        "ok": True, "window_days": cfg["window_days"],
        "cost_pct_assumed": cost, "scored_observations": len(scored),
        "cost_autopsy": {**overall, "verdict": autopsy_verdict},
        "positive_slices": positive[:12],
        "slices": slices,
    }


@router.get("/funnel")
async def promotion_funnel(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Shadow-collection funnel diagnostic (2026-08 — 'it's not
    recording the 20 it needs'). Shows every link between intent
    creation and the promotion gate so a starved gate can be traced
    to the exact dead stage. Shadow observations exist ONLY when an
    intent survives all upstream gates and is blocked by exit_only at
    the broker router — a disarmed master switch kills collection."""
    from datetime import timedelta  # noqa: WPS433
    from shared.execution_mode import SHADOW_FILLS, get_entry_mode_config  # noqa: WPS433
    from shared.forensics.promotion_gate import gate_status  # noqa: WPS433
    from shared.risk_sizer.missed_entries import COLLECTION  # noqa: WPS433

    cut = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    try:
        from shared.auto_router import _is_master_switch_armed  # noqa: WPS433
        armed = bool(await _is_master_switch_armed())
    except Exception:  # noqa: BLE001
        armed = None
    mode = (await get_entry_mode_config()).get("mode")

    base = {"action": "BUY", "ingest_ts": {"$gte": cut}}
    n_created = await db["shared_intents"].count_documents(base, maxTimeMS=8000)
    n_master_blocked = await db["shared_intents"].count_documents(
        {**base, "broker_reason": "master_switch_disarmed"}, maxTimeMS=8000)
    n_exit_only = await db["shared_intents"].count_documents(
        {**base, "broker_reason": {"$regex": "exit_only_mode"}}, maxTimeMS=8000)
    n_expired_unrouted = await db["shared_intents"].count_documents(
        {**base, "broker_reason": {"$regex": "^EXPIRED"}}, maxTimeMS=8000)
    # router attempts leave an executions receipt (blocked or filled) —
    # broker_reason alone is unreliable (TTL expiry also writes it)
    n_reached_router = await db["executions"].count_documents(
        {"ts": {"$gte": cut}, "action": "BUY"}, maxTimeMS=8000)
    top_blockers = await db["shared_intents"].aggregate([
        {"$match": {**base, "gate_state": {"$in": ["blocked", "advisory_only",
                                                   "expired_unrouted"]}}},
        {"$group": {"_id": {"$ifNull": ["$broker_reason", "$risk_reason"]},
                    "n": {"$sum": 1}}},
        {"$sort": {"n": -1}}, {"$limit": 8},
    ], maxTimeMS=8000).to_list(8)
    n_shadow = await db[SHADOW_FILLS].count_documents(
        {"ts": {"$gte": cut}, "_id": {"$not": {"$regex": "^shadow-test"}}},
        maxTimeMS=8000)
    _counted = "exit_only_mode|insufficient_balance|no_balance_no_trade"
    n_ledger = await db[COLLECTION].count_documents(
        {"block_reason": {"$regex": _counted}}, maxTimeMS=8000)
    n_scored = await db[COLLECTION].count_documents(
        {"block_reason": {"$regex": _counted},
         "outcome": {"$in": ["tp_hit", "sl_hit", "expired"]}}, maxTimeMS=8000)
    gate = await gate_status()

    if armed is False:
        verdict = ("COLLECTION DEAD: master switch is DISARMED — intents die at "
                   "stage 1 and never reach the router where exit_only records "
                   "shadows. ARM the master switch; exit_only still blocks every "
                   "real entry, so no money can move.")
    elif n_created == 0:
        verdict = "COLLECTION DEAD: no BUY intents created in 7d — brains/pulse not emitting."
    elif n_expired_unrouted >= max(n_created - n_expired_unrouted, 1):
        verdict = ("COLLECTION DEAD: most intents EXPIRE before routing (TTL) — "
                   "the router queue isn't being consumed; master switch or "
                   "pulse arm state is off even if the flag reads armed.")
    elif n_reached_router == 0 and n_exit_only == 0:
        verdict = ("COLLECTION DEAD: intents exist but none reach the broker "
                   "router — upstream gates (seat/risk/timing) kill everything; "
                   "see top_blockers.")
    elif n_exit_only == 0:
        verdict = ("STARVED: intents reach the router but none are blocked by "
                   "exit_only — check execution mode (currently "
                   f"{mode}).")
    elif n_scored == 0:
        verdict = ("STALLED: shadows recorded but none scored yet — missed-entry "
                   "resolver needs bars + the horizon to elapse; if this persists "
                   ">24h check bar coverage for the blocked symbols.")
    else:
        need = gate.get("per_lane", {})
        verdict = (f"FLOWING: {n_scored} scored observations. Per-lane progress: "
                   + ", ".join(f"{k}: {v.get('n', 0)}" for k, v in need.items()))

    return {
        "ok": True, "window_days": 7,
        "master_switch_armed": armed, "execution_mode": mode,
        "funnel": {
            "buy_intents_created": n_created,
            "blocked_master_switch": n_master_blocked,
            "expired_before_routing": n_expired_unrouted,
            "reached_broker_router": n_reached_router,
            "blocked_by_exit_only": n_exit_only,
            "shadow_fills_recorded": n_shadow,
            "ledger_rows_exit_only": n_ledger,
            "scored_observations": n_scored,
        },
        "top_blockers": [{"reason": str(b["_id"])[:120], "n": b["n"]}
                         for b in top_blockers],
        "gate_per_lane": gate.get("per_lane"),
        "verdict": verdict,
    }
