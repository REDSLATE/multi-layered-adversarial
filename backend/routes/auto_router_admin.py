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

Both endpoints are admin-JWT-only — no runtime token bypass.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from auth import get_current_user
from shared.auto_router import force_one_tick, get_status


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
