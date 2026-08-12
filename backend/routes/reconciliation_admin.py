"""Broker-fill reconciliation admin (2026-06 operator directive).

If the broker says a real fill occurred, Mission Control must show
either the resulting completed outcome or an explicit unresolved
reconciliation exception. Never a silent disappearance.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth import get_current_user
from db import db

router = APIRouter(prefix="/admin/reconciliation", tags=["reconciliation"])


@router.get("")
async def reconciliation_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.reconciliation import (  # noqa: WPS433
        LEDGER, OUTCOMES, STATE_FLAG, reconciliation_counters,
    )
    counters = await reconciliation_counters()
    state = await db["runtime_flags"].find_one(
        {"_id": STATE_FLAG}, {"_id": 0, "last_run": 1}) or {}
    unresolved = await db[LEDGER].find(
        {"link.status": {"$in": ["unlinked", "retry", "unmatched_internal"]}},
        {"symbol": 1, "side": 1, "qty": 1, "price": 1, "ts": 1, "broker": 1,
         "order_id": 1, "link": 1},
    ).sort("ts", -1).max_time_ms(5000).to_list(20)
    recent = await db[OUTCOMES].find(
        {}, {"symbol": 1, "lane": 1, "qty": 1, "entry_avg_price": 1,
             "exit_price": 1, "realized_pnl_usd": 1, "net_return_pct": 1,
             "fees_usd": 1, "fee_pct": 1, "holding_s": 1, "brain": 1,
             "stack": 1, "intent_id": 1, "epoch_id": 1, "exit_ts": 1,
             "measured_cost_eligible": 1, "round_trip_cost_pct": 1},
    ).sort("exit_ts", -1).max_time_ms(5000).to_list(10)
    return {"ok": True, "counters": counters,
            "last_run": state.get("last_run"),
            "unresolved": unresolved, "recent_outcomes": recent}


class RunBody(BaseModel):
    full: bool = False


@router.post("/run")
async def reconciliation_run(
    body: RunBody,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Run now. full=true = historical backfill (walks the entire
    broker trade history) and returns the backfill report."""
    from shared.reconciliation import run_reconciliation  # noqa: WPS433
    report = await run_reconciliation(full=body.full)
    return {"ok": True, "report": report}


@router.get("/orphans")
async def orphan_exits(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Orphan SELL fills (no recorded entry lots) + candidate adopted
    positions for manual matching."""
    from shared.reconciliation import LEDGER  # noqa: WPS433
    orphans = await db[LEDGER].find(
        {"link.exit_orphan": True, "link.orphan_resolved": {"$ne": True}},
        {"symbol": 1, "side": 1, "qty": 1, "price": 1, "ts": 1, "broker": 1,
         "lane": 1, "fee_usd": 1, "order_id": 1},
    ).sort("ts", -1).max_time_ms(5000).to_list(30)
    out = []
    for f in orphans:
        candidates = await db["shared_live_positions"].find(
            {"symbol": f.get("symbol"),
             "opened_at": {"$lte": str(f.get("ts") or "9999")}},
            {"position_id": 1, "intent_id": 1, "opened_at": 1, "stack": 1,
             "opened_notional_usd": 1, "state": 1},
        ).sort("opened_at", -1).max_time_ms(3000).to_list(3)
        for c in candidates:
            c["_id"] = str(c["_id"])
        out.append({**f, "candidates": candidates})
    return {"ok": True, "orphans": out,
            "note": ("resolve by supplying the entry cost basis — the "
                     "outcome counts in realized P&L but is NOT "
                     "measured-cost eligible (entry fees unknown)")}


class OrphanResolveBody(BaseModel):
    fill_id: str
    entry_price: float
    entry_ts: Optional[str] = None
    note: Optional[str] = ""
    position_id: Optional[str] = None
    intent_id: Optional[str] = None


@router.post("/orphans/resolve")
async def orphan_resolve(
    body: OrphanResolveBody,
    user: dict = Depends(get_current_user),  # noqa: B008
):
    from fastapi import HTTPException  # noqa: WPS433
    from shared.reconciliation import resolve_orphan_exit  # noqa: WPS433
    try:
        outcome = await resolve_orphan_exit(
            body.fill_id, body.entry_price,
            operator=user.get("email") or "operator",
            entry_ts=body.entry_ts, note=body.note or "",
            position_id=body.position_id, intent_id=body.intent_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "outcome": outcome}


@router.get("/repeat-scorecard")
async def repeat_scorecard(
    days: float = 7,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Rank brains/stacks by duplicate signal emission: intents that
    re-fire the same (symbol, hour) cluster. The noisiest source is
    the one to tune upstream."""
    from datetime import datetime, timedelta, timezone  # noqa: WPS433
    cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    clusters = await db["shared_intents"].aggregate([
        {"$match": {"created_at": {"$gte": cut},
                    "action": {"$in": ["BUY", "SELL"]}}},
        {"$group": {"_id": {"stack": {"$ifNull": ["$brain", "$stack"]},
                            "symbol": "$symbol",
                            "hour": {"$substr": ["$created_at", 0, 13]},
                            "action": "$action"},
                    "n": {"$sum": 1}}},
    ], maxTimeMS=10000).to_list(20000)
    board: dict[str, dict] = {}
    for c in clusters:
        stack = str(c["_id"].get("stack") or "unattributed")
        b = board.setdefault(stack, {"intents": 0, "clusters": 0,
                                     "worst": None})
        b["intents"] += c["n"]
        b["clusters"] += 1
        rep = c["n"] - 1
        if rep > 0 and (b["worst"] is None or rep > b["worst"]["repeats"]):
            b["worst"] = {"symbol": c["_id"].get("symbol"),
                          "hour": c["_id"].get("hour"),
                          "action": c["_id"].get("action"),
                          "repeats": rep}
    rows = []
    for stack, b in board.items():
        repeats = b["intents"] - b["clusters"]
        rows.append({
            "stack": stack,
            "intents": b["intents"],
            "independent_clusters": b["clusters"],
            "repeats": repeats,
            "repeat_ratio": round(repeats / b["intents"], 3)
            if b["intents"] else 0,
            "worst_cluster": b["worst"],
        })
    rows.sort(key=lambda r: (-r["repeats"], -r["repeat_ratio"]))
    return {"ok": True, "window_days": days, "scorecard": rows,
            "note": ("repeats = same brain re-emitting the same "
                     "symbol within the same hour; dedup already "
                     "collapses these in learning — tune the worst "
                     "emitter at the source")}
@router.get("/repeat-intents")
async def repeat_intents(
    days: float = 7,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Evidence for the multi-brain repeat hypothesis: how many
    observations were suppressed as repeats (post-dedup) and which
    stacks/symbols re-emit the same signal."""
    from datetime import datetime, timedelta, timezone  # noqa: WPS433
    cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    agg = await db["missed_entry_outcomes"].aggregate([
        {"$match": {"blocked_at": {"$gte": cut},
                    "repeat_count": {"$gt": 0}}},
        {"$group": {"_id": "$symbol",
                    "observations": {"$sum": 1},
                    "repeats_suppressed": {"$sum": "$repeat_count"},
                    "stacks": {"$addToSet": "$stack"},
                    "repeat_stacks": {"$addToSet": "$repeat_stacks"}}},
        {"$sort": {"repeats_suppressed": -1}}, {"$limit": 12},
    ], maxTimeMS=8000).to_list(12)
    total_obs = await db["missed_entry_outcomes"].count_documents(
        {"blocked_at": {"$gte": cut}})
    total_suppressed = 0
    for a in agg:
        total_suppressed += a.get("repeats_suppressed") or 0
        a["repeat_stacks"] = sorted({s for grp in (a.get("repeat_stacks")
                                                   or []) for s in (grp or [])})
    return {"ok": True, "window_days": days,
            "independent_observations": total_obs,
            "repeats_suppressed": total_suppressed,
            "by_symbol": agg,
            "note": ("repeats are same-symbol signals within the dedup "
                     "window — recorded as repeat_count on the ONE "
                     "independent observation instead of new rows")}
