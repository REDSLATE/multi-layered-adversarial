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
