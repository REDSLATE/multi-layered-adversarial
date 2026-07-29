"""Retention sweeper introspection + manual-run endpoints.

* `GET  /api/admin/retention/status` — worker liveness, last cycle
  stats, rules table. Cheap, read-only.
* `POST /api/admin/retention/run` — run one purge cycle now (bounded
  batches; safe to call while the scheduled loop is idle — a second
  concurrent call short-circuits with `cycle already running`).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from auth import get_current_user
from shared.retention import get_status, run_cycle

router = APIRouter(prefix="/admin/retention", tags=["admin-retention"])


@router.get("/status")
async def retention_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    return get_status()


@router.post("/run")
async def retention_run(_user: dict = Depends(get_current_user)):  # noqa: B008
    return await run_cycle()


@router.get("/health")
async def retention_health(_user: dict = Depends(get_current_user)):  # noqa: B008
    """Per-collection growth verdict (2026-07-29): current O(1)
    estimated counts vs the ~24h baseline snapshot — flags any RULES
    collection accumulating rows despite its retention rule."""
    from shared.retention_health import evaluate  # noqa: WPS433
    return await evaluate()
