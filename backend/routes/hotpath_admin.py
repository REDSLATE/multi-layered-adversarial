"""Hot-path admin — Atlas outbox visibility + operator controls."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from auth import get_current_user
from shared.hotpath import outbox
from shared.hotpath.handlers import register_all

router = APIRouter(prefix="/admin/hotpath", tags=["hotpath"])

register_all()


@router.get("/outbox")
async def outbox_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    return {
        "status": outbox.get_status(),
        "dead_letters": outbox.dead_letters(),
    }


@router.post("/outbox/drain")
async def outbox_drain(_user: dict = Depends(get_current_user)):  # noqa: B008
    return {"ok": True, "result": await outbox.drain_once()}


@router.post("/outbox/retry-dead")
async def outbox_retry_dead(_user: dict = Depends(get_current_user)):  # noqa: B008
    return {"ok": True, "reset": outbox.retry_dead_letters()}


# ── ExecutionPolicySnapshot + local queue/spend (2026-07-24) ────────

@router.get("/policy")
async def policy_snapshot_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.hotpath import daily_spend, policy_snapshot
    return {
        "snapshot": policy_snapshot.get_status(),
        "daily_spend": daily_spend.get_status(),
    }


@router.post("/policy/refresh")
async def policy_snapshot_refresh(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.hotpath import policy_snapshot
    snap = await policy_snapshot.refresh_from_atlas()
    return {"ok": True, "version": snap.get("version"),
            "degraded_keys": snap.get("degraded_keys") or []}


@router.get("/queue")
async def intent_queue_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.hotpath import intent_queue
    return {"queue": intent_queue.get_status()}
