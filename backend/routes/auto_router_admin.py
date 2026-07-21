"""Auto-router introspection + manual-tick endpoints.

Added 2026-06-09 to answer the operator's question "why aren't trades
firing even though all gates pass?". The auto-router is the only
process that promotes a `dry_run_passed` intent to a real broker
order; if its async task is dead or stalled, the entire fleet falls
back to dry-runs only. Before this module existed, the only way to
confirm the task's liveness was to read pod logs — which the operator
can't do on a deployed environment.

Endpoints:

* `GET  /api/admin/auto-router/status` — task liveness, tick counters,
  last error. Cheap, read-only, no broker calls. Safe to poll.
* `POST /api/admin/auto-router/force-tick` — run one tick out of band.
  Returns the list of intents the tick touched (executed / no_trade /
  observation / advisory). Useful right after flipping a gate when you
  don't want to wait `interval_sec` for the scheduled tick.
* `POST /api/admin/auto-router/start` — flip the `auto_router_enabled`
  runtime flag ON and start the task immediately (if not already
  running). The flag persists across pod restarts in the
  `runtime_flags` collection.
* `POST /api/admin/auto-router/stop` — flip the flag OFF. The current
  task is left to finish its tick gracefully (no-op for any future
  pickup; the loop reads the flag at the top of each tick).

Both endpoints are admin-JWT-only — no runtime token bypass.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db
from shared.auto_router import force_one_tick, get_status, start_auto_router_if_enabled


router = APIRouter(prefix="/admin/auto-router", tags=["admin-auto-router"])


@router.get("/status")
async def auto_router_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Snapshot of the auto-router's running task.

    Read this when the operator's question is *"my gates are open but
    nothing is firing — is the router even running?"*. Returns:

    - `task_alive`: True if the asyncio.Task is still scheduled
    - `task_done`: True if the task has exited (cancelled, crashed,
      or never started)
    - `task_exception`: repr of the exception that killed the task,
      if any
    - `tick_count` / `last_tick_ts` / `last_tick_results` /
      `last_tick_executed` / `last_tick_error`: tick heartbeat data
    - `enabled_env`: value of `AUTO_ROUTER_ENABLED` at boot

    If `task_alive=False`, no autonomous orders will fire — only the
    manual `/api/execution/submit` path works.

    If `task_alive=True` but `last_tick_ts` is stale (older than
    ~2× `interval_sec`), the tick is stuck — pod restart recovers.
    """
    return get_status()


@router.get("/pick-probe")
async def auto_router_pick_probe(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Diagnose 'router ticking but 0 picked'. Runs the router's EXACT
    pick query, then applies its filters cumulatively so the step where
    the count collapses exposes exactly which filter kills the match on
    THIS environment's data. Read-only, bounded, safe to poll."""
    import os  # noqa: WPS433
    from datetime import timedelta  # noqa: WPS433
    try:
        lookback_min = int(os.environ.get("AUTO_ROUTER_LOOKBACK_MIN", "60"))
    except (TypeError, ValueError):
        lookback_min = 60
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=lookback_min)
    ).isoformat()
    filters = [
        ("window", {"ingest_ts": {"$gte": cutoff}}),
        ("not_executed", {"executed": {"$ne": True}}),
        ("directional_action", {"action": {"$in": ["BUY", "SELL", "SHORT", "COVER"]}}),
        ("has_symbol", {"symbol": {"$ne": None}}),
        ("gate_state_open", {"gate_state": {"$nin": [
            "blocked", "no_trade", "advisory_only", "submitted",
            "expired_unrouted",
        ]}}),
        ("not_poisoned", {"route_timeouts": {"$not": {"$gte": 3}}}),
    ]
    q: dict = {}
    breakdown = []
    for name, f in filters:
        q.update(f)
        try:
            n = await db["shared_intents"].count_documents(dict(q), maxTimeMS=6000)
            breakdown.append({"filter_added": name, "count": n})
        except Exception as exc:  # noqa: BLE001
            breakdown.append({"filter_added": name, "error": str(exc)[:150]})
    sample = []
    try:
        sample = await (
            db["shared_intents"]
            .find(q, {"_id": 0, "intent_id": 1, "symbol": 1, "action": 1,
                      "lane": 1, "gate_state": 1, "ingest_ts": 1,
                      "executed": 1, "route_timeouts": 1})
            .sort("ingest_ts", -1)
            .max_time_ms(6000)
            .to_list(3)
        )
    except Exception as exc:  # noqa: BLE001
        sample = [{"error": str(exc)[:150]}]
    counts = [b.get("count") for b in breakdown if "count" in b]
    routable_now = counts[-1] if counts else None
    return {
        "lookback_min": lookback_min,
        "cutoff": cutoff,
        "filter_breakdown": breakdown,
        "routable_now": routable_now,
        "sample_would_pick": sample,
        "router_status": get_status(),
        "reading": (
            "routable_now > 0 with last_tick 0 picked → check "
            "router_status.last_intent_error (_route_one crashing). "
            "routable_now == 0 → the filter step where count collapses "
            "is the killer."
        ),
    }


@router.post("/force-tick")
async def auto_router_force_tick(
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Run a single tick of the auto-router out of band.

    Use this right after you've just unblocked a gate (lane toggle,
    ladder promotion, executor seat rotation) and want the queue
    drained immediately instead of waiting up to `interval_sec` for
    the scheduled tick.

    Returns the same `{verdict, intent_id, reason, ...}` shape per
    intent that the scheduled loop produces — including any orders
    that hit the broker on THIS call.

    Doctrine: this calls the same `_tick()` as the scheduled loop,
    so every safety gate (sizing, broker freeze, lane toggle, exposure
    caps, executor seat) still applies. There is no "force-trade"
    semantic — only "drain the queue now."
    """
    return await force_one_tick()


@router.post("/start")
async def auto_router_start(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Flip `runtime_flags.auto_router_enabled = true` AND start the
    background task immediately.

    This is the safe alternative to making the auto-router boot
    unconditionally — on 2026-02-19 an unconditional boot crashed
    the prod pod (520 across all authed endpoints). With this
    endpoint, the operator can flip on a healthy pod, watch the
    `/status` endpoint, and POST `/stop` if anything starts to
    smell wrong, without redeploying.
    """
    now = datetime.now(timezone.utc).isoformat()
    await db["runtime_flags"].update_one(
        {"_id": "auto_router_enabled"},
        {"$set": {"enabled": True, "updated_at": now, "updated_by": _user.get("email") or "unknown"}},
        upsert=True,
    )
    try:
        start_auto_router_if_enabled()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "flag": "enabled"}
    return {"ok": True, "flag": "enabled", "updated_at": now}


@router.post("/stop")
async def auto_router_stop(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Flip `runtime_flags.auto_router_enabled = false`.

    The running task is not interrupted mid-tick — it will simply
    not be started on the next pod boot. To stop a runaway task
    immediately, also flip the master trading switch off
    (POST /admin/trading/toggle {enabled: false, reason: ...}).
    """
    now = datetime.now(timezone.utc).isoformat()
    await db["runtime_flags"].update_one(
        {"_id": "auto_router_enabled"},
        {"$set": {"enabled": False, "updated_at": now, "updated_by": _user.get("email") or "unknown"}},
        upsert=True,
    )
    return {"ok": True, "flag": "disabled", "updated_at": now}


@router.get("/conviction-floor")
async def get_conviction_floor_state(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Effective conviction multiplier floor + its source.

    `runtime_flags._id=conviction_floor` (operator knob) beats the
    `AUTO_ROUTER_MIN_CONVICTION_MULT` env default. 0 disables the
    floor entirely (weak intents die SIZED_TO_ZERO again)."""
    from shared.auto_router_stages import _min_conviction_mult, get_conviction_floor
    doc = await db["runtime_flags"].find_one(
        {"_id": "conviction_floor"}, {"_id": 0},
    )
    return {
        "floor": await get_conviction_floor(),
        "source": "operator_knob" if doc and doc.get("value") is not None else "env_default",
        "env_default": _min_conviction_mult(),
        "updated_at": (doc or {}).get("updated_at"),
        "updated_by": (doc or {}).get("updated_by"),
    }


@router.post("/conviction-floor")
async def set_conviction_floor(
    body: dict,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Set the floor (0.0-1.0). 0 disables. Takes effect within one
    tick (15s cache TTL is invalidated on write)."""
    from fastapi import HTTPException
    from shared.auto_router_stages import invalidate_conviction_floor_cache
    try:
        value = float(body.get("value"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="value must be a number 0.0-1.0")
    if not (0.0 <= value <= 1.0):
        raise HTTPException(status_code=422, detail="value must be within 0.0-1.0")
    now = datetime.now(timezone.utc).isoformat()
    prev_doc = await db["runtime_flags"].find_one(
        {"_id": "conviction_floor"}, {"value": 1},
    )
    await db["runtime_flags"].update_one(
        {"_id": "conviction_floor"},
        {"$set": {
            "value": value, "updated_at": now,
            "updated_by": _user.get("email") or "unknown",
        }},
        upsert=True,
    )
    # Change log (2026-07-21): so floor tuning can be correlated with
    # fill outcomes. Small collection, not retention-purged.
    try:
        await db["conviction_floor_history"].insert_one({
            "value": value,
            "prev": (prev_doc or {}).get("value"),
            "updated_by": _user.get("email") or "unknown",
            "ts": now,
        })
    except Exception:  # noqa: BLE001
        pass
    invalidate_conviction_floor_cache()
    return {"ok": True, "floor": value, "updated_at": now}


@router.get("/conviction-floor/history")
async def conviction_floor_history(
    limit: int = 10,
    _user: dict = Depends(get_current_user),  # noqa: B008
):
    """Last N floor adjustments, newest first."""
    limit = max(1, min(50, limit))
    rows = await (
        db["conviction_floor_history"]
        .find({}, {"_id": 0})
        .sort("ts", -1)
        .limit(limit)
        .to_list(limit)
    )
    return {"history": rows}
