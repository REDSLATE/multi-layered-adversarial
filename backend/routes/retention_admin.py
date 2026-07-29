"""Retention sweeper introspection + manual-run endpoints.

* `GET  /api/admin/retention/status` — worker liveness, last cycle
  stats, rules table, plus the retention-health growth evaluation
  (per-collection estimated count vs. trailing baseline). Read-only.
* `GET  /api/admin/retention/health` — the growth evaluation alone,
  for operators/monitors that only want the anomaly signal.
* `POST /api/admin/retention/run` — run one purge cycle now (bounded
  batches; safe to call while the scheduled loop is idle — a second
  concurrent call short-circuits with `cycle already running`).
* `POST /api/admin/retention/health/sample` — take a count snapshot
  now instead of waiting for the weekly cadence (seeds the baseline).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from auth import get_current_user
from shared.retention import (
    evaluate_retention_health,
    get_status,
    run_cycle,
    sample_retention_counts,
)

router = APIRouter(prefix="/admin/retention", tags=["admin-retention"])


@router.get("/status")
async def retention_status(_user: dict = Depends(get_current_user)):  # noqa: B008
    status = get_status()
    status["health_evaluation"] = await evaluate_retention_health()
    return status


@router.get("/health")
async def retention_health(_user: dict = Depends(get_current_user)):  # noqa: B008
    return await evaluate_retention_health()


@router.post("/run")
async def retention_run(_user: dict = Depends(get_current_user)):  # noqa: B008
    return await run_cycle()


@router.post("/health/sample")
async def retention_health_sample(_user: dict = Depends(get_current_user)):  # noqa: B008
    return await sample_retention_counts()
