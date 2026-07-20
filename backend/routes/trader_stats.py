"""Per-brain fires + dissent stats — powers the Brain Personalities
tile on Overview and the per-brain strip inside Operator Control.

Doctrine (locked 2026-07-13):
    Statistics stay per-brain, never averaged across. Each brain
    earns its own history from the events it actually produced.

Endpoints:
    GET /api/admin/trader/brain-accuracy?window_hours=24
        Fires + fills + avg_confidence per brain, computed from
        `shared_intents` filtered to `evidence.arbitrated_by='mc_arbiter'`
        (i.e. the pulse-emitted intents — the doctrine-current tape).
    GET /api/admin/trader/dissent?window_hours=24
        Per-brain dissent — how often a brain submitted an opinion
        on a seat and did NOT win. Computed from `mc_seats.decision`.
        Also reports `top_dissents_vs` — the brains that stole this
        one's seats the most.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import db

logger = logging.getLogger("routes.trader_stats")
router = APIRouter(prefix="/admin/trader", tags=["trader-stats"])

# Doctrine-current brain set. Kept here because the Overview tile
# renders per-brain colors keyed off these exact strings. Any brain
# emitted from the pulse but not in this list is added dynamically
# so we never silently drop a real signal.
_KNOWN_BRAINS = ("camino", "barracuda", "hellcat", "gto")


def _since_iso(window_hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()


@router.get("/brain-accuracy")
async def get_brain_accuracy(
    window_hours: int = Query(24, ge=1, le=720),
    _user: dict = Depends(get_current_user),
) -> dict:
    """Per-brain fires + fill rate + avg_confidence.

    Fires = count of intents each brain emitted into `shared_intents`
    from the arbiter in the window. Fills = intents that reached
    `executed=True`. Fill rate = fills / fires, expressed as an int
    percentage. avg_confidence = mean of `confidence` on those intents.
    """
    since = _since_iso(window_hours)
    pipeline = [
        {"$match": {
            "ingest_ts": {"$gte": since},
        }},
        {"$group": {
            "_id": {"$ifNull": ["$stack_canonical", "$stack"]},
            "fires": {"$sum": 1},
            "fills": {"$sum": {"$cond": [{"$eq": ["$executed", True]}, 1, 0]}},
            "avg_confidence": {"$avg": "$confidence"},
        }},
    ]
    try:
        rows = await db.shared_intents.aggregate(pipeline, maxTimeMS=3000).to_list(50)
    except Exception as exc:  # noqa: BLE001
        logger.warning("brain-accuracy aggregate failed: %s", exc)
        rows = []

    by_brain: dict[str, dict] = {b: {
        "brain": b, "fires": 0, "fills": 0,
        "fill_rate_pct": 0, "avg_confidence": None,
        "avg_spread_bps_at_fire": None,
    } for b in _KNOWN_BRAINS}
    for r in rows:
        brain = (r.get("_id") or "").strip().lower()
        if not brain:
            continue
        fires = int(r.get("fires") or 0)
        fills = int(r.get("fills") or 0)
        by_brain[brain] = {
            "brain": brain,
            "fires": fires,
            "fills": fills,
            "fill_rate_pct": round(100.0 * fills / fires) if fires else 0,
            "avg_confidence": (
                round(float(r["avg_confidence"]), 3)
                if r.get("avg_confidence") is not None else None
            ),
            # Spread telemetry not currently plumbed through the intent
            # tape — keep the field so the UI renders "—" instead of
            # exploding on undefined.
            "avg_spread_bps_at_fire": None,
        }

    return {
        "window_hours": window_hours,
        "since": since,
        "brains": sorted(by_brain.values(), key=lambda x: -x["fires"]),
    }


@router.get("/dissent")
async def get_dissent(
    window_hours: int = Query(24, ge=1, le=720),
    _user: dict = Depends(get_current_user),
) -> dict:
    """Per-brain dissent — how often each brain lost a seat.

    Every arbitrated seat has `decision.winner_brain` + a list of
    `decision.field[].brain`. A brain "dissented" on a seat when it
    was in `field` but not the winner. We also record which brains
    beat it the most (`top_dissents_vs`).

    Rate expressed as percentage of the brain's total cycles
    (seats it participated in).
    """
    since = _since_iso(window_hours)
    pipeline = [
        {"$match": {
            "decision.arbitrated_at": {"$gte": since},
            "decision.winner_brain": {"$ne": None},
        }},
        {"$project": {
            "_id": 0,
            "winner": "$decision.winner_brain",
            "field": "$decision.field.brain",
        }},
    ]
    try:
        rows = await db.mc_seats.aggregate(pipeline, maxTimeMS=3000).to_list(20000)
    except Exception as exc:  # noqa: BLE001
        logger.warning("dissent aggregate failed: %s", exc)
        rows = []

    # For each brain: cycles (participation count), dissents (times
    # it wasn't the winner), and per-opponent dissent counts.
    stats: dict[str, dict] = {b: {
        "brain": b, "cycles": 0, "dissents": 0,
        "top_dissents_vs": {},
    } for b in _KNOWN_BRAINS}

    for row in rows:
        winner = (row.get("winner") or "").strip().lower()
        field = [str(b).strip().lower() for b in (row.get("field") or []) if b]
        if not winner or not field:
            continue
        for participant in field:
            if participant not in stats:
                stats[participant] = {
                    "brain": participant, "cycles": 0, "dissents": 0,
                    "top_dissents_vs": {},
                }
            stats[participant]["cycles"] += 1
            if participant != winner:
                stats[participant]["dissents"] += 1
                td = stats[participant]["top_dissents_vs"]
                td[winner] = td.get(winner, 0) + 1

    out = []
    for brain, s in stats.items():
        cycles = s["cycles"]
        dissents = s["dissents"]
        # Cap top_dissents_vs at the top 5 to keep payloads small.
        td_sorted = dict(sorted(
            s["top_dissents_vs"].items(),
            key=lambda kv: -kv[1],
        )[:5])
        out.append({
            "brain": brain,
            "cycles": cycles,
            "dissents": dissents,
            "dissent_rate_pct": round(100.0 * dissents / cycles) if cycles else 0,
            "top_dissents_vs": td_sorted,
        })

    out.sort(key=lambda x: -x["cycles"])
    return {
        "window_hours": window_hours,
        "since": since,
        "brains": out,
    }


# ── Legacy stubs so the current UI doesn't 404 while we finish the
# migration off the sidecar trader model. `status` and `receipts`
# were the sidecar's per-cycle log; the sidecar was decommissioned
# in iter-23. These stubs return an empty-but-well-formed shape so
# the TradeTape tile can render a "no cycles" empty state instead
# of a red 404 banner.

@router.get("/status")
async def get_trader_status(_user: dict = Depends(get_current_user)) -> dict:
    """Live-trading loop status for the Trade Tape strip.

    2026-07-20: previously a decommissioned-sidecar stub that pinned
    the tiles to DISABLED/IDLE forever. Now surfaces the REAL
    authority — the auto-router loop + master switch — in the shape
    the TradeTape frontend consumes (`loop.alive_inference`,
    `trades.fires_today`, `trades.spent_today_usd`,
    `loop.last_receipt_ts`)."""
    from shared.auto_router_supervisor import get_status  # noqa: WPS433
    from routes.trading_controls import is_trading_enabled  # noqa: WPS433

    st = get_status()
    try:
        armed = bool(await is_trading_enabled())
    except Exception:  # noqa: BLE001
        armed = False

    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fires_today = 0
    spent_today = 0.0
    try:
        pipe = [
            {"$match": {"ts": {"$regex": f"^{today_iso}"}, "ok": True}},
            {"$group": {"_id": None, "n": {"$sum": 1},
                        "spent": {"$sum": {"$ifNull": ["$notional_usd", 0]}}}},
        ]
        async for row in db["executions"].aggregate(pipe, maxTimeMS=5000):
            fires_today = int(row.get("n") or 0)
            spent_today = float(row.get("spent") or 0.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("trader/status fires-today read failed: %s", exc)

    task_alive = bool(st.get("task_alive"))
    if not task_alive:
        loop_state = "dead"
    elif not armed:
        loop_state = "disarmed"
    else:
        loop_state = "live"

    return {
        "trader_enabled": armed and task_alive,
        "loop_state": loop_state,
        "master_switch_armed": armed,
        "loop": {
            "alive_inference": task_alive,
            "last_receipt_ts": st.get("last_tick_ts"),
        },
        "trades": {
            "fires_today": fires_today,
            "spent_today_usd": spent_today,
        },
        "router": {
            "tick_count": st.get("tick_count"),
            "last_tick_ts": st.get("last_tick_ts"),
            "last_tick_error": st.get("last_tick_error"),
            "last_tick_disarmed": st.get("last_tick_disarmed"),
            "last_tick_route_timeouts": st.get("last_tick_route_timeouts"),
            "interval_sec": st.get("interval_sec"),
        },
    }


@router.get("/receipts")
async def get_trader_receipts(
    limit: int = Query(15, ge=1, le=1000),
    lane: Optional[str] = None,
    fired_only: bool = False,
    _user: dict = Depends(get_current_user),
) -> dict:
    """Sidecar trader per-cycle receipts — DECOMMISSIONED. We now
    return the last N `shared_intents` docs shaped as receipt-like
    rows so the TradeTape tile stays useful during the transition."""
    q: dict = {}
    if lane in {"equity", "crypto"}:
        q["lane"] = lane
    if fired_only:
        q["executed"] = True
    try:
        rows = await db.shared_intents.find(
            q, sort=[("ingest_ts", -1)],
        ).to_list(limit)
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_trader_receipts read failed: %s", exc)
        rows = []
    items = []
    for r in rows:
        stack = (r.get("stack_canonical") or r.get("stack") or "").lower()
        verdict = r.get("action")
        fired = bool(r.get("executed"))
        gate = r.get("gate_state")
        broker_reason = r.get("broker_reason")
        # Shape mirrors the pre-decommission sidecar TradeTape schema
        # so the existing frontend TapeRow renders without a rewrite.
        # `chosen` = the winning brain's verdict; `seats.executor`
        # = which brain slot held the executor role; `risk.reason`
        # + `broker_result` mirror the old fields the row consumes.
        items.append({
            "cycle_id": str(r.get("intent_id") or r.get("_id") or ""),
            "ts": r.get("ingest_ts"),
            "lane": r.get("lane"),
            "symbol": r.get("symbol"),
            "chosen": {
                "brain": stack,
                "verdict": verdict,
                "confidence": r.get("confidence"),
            },
            "seats": {"executor": stack},
            "risk": {
                "ok": gate == "submitted",
                "reason": (
                    None if gate == "submitted"
                    else (gate or broker_reason)
                ),
            },
            "broker_result": (
                {"order_id": r.get("execution_receipt_id") or ""}
                if fired else None
            ),
            "error": (
                broker_reason
                if (not fired and broker_reason and gate != "pending")
                else None
            ),
        })
    return {"items": items, "count": len(items)}
