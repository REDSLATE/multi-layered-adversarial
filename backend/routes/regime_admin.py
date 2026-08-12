"""Regime Engine admin routes (2026-06 operator directive).

Advisory context layer — V1 has ZERO sizing/gating authority.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth import get_current_user
from db import db

router = APIRouter(prefix="/admin/regime", tags=["regime"])


@router.get("/state")
async def regime_state(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.regime.snapshot import LANES, get_cached, worker_status
    return {"ok": True,
            "lanes": {lane: get_cached(lane) for lane in LANES},
            "worker": worker_status(),
            "advisory_only": True}


@router.get("/history")
async def regime_history(lane: str = "equity", limit: int = 120,
                         _user: dict = Depends(get_current_user)):  # noqa: B008
    rows = await db["regime_snapshots"].find(
        {"lane": lane},
        {"_id": 0, "lane": 1, "created_at": 1, "feature_asof": 1,
         "top_state": 1, "top_label": 1, "probs": 1, "entropy": 1,
         "model_version": 1, "agreement": 1},
    ).sort("created_at", -1).max_time_ms(5000).to_list(min(int(limit), 500))
    return {"ok": True, "lane": lane, "rows": rows}


@router.get("/brain-matrix")
async def regime_brain_matrix(lane: Optional[str] = None,
                              all_versions: bool = False,
                              _user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.regime.brain_matrix import compute_matrix
    result = await asyncio.to_thread(compute_matrix, lane, all_versions)
    return {"ok": True, **result}


@router.get("/model-info")
async def regime_model_info(lane: str = "equity",
                            _user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.regime.snapshot import get_model_meta
    meta = await asyncio.to_thread(get_model_meta, lane)
    return {"ok": meta is not None, "lane": lane, "model": meta}


@router.get("/edge-preview")
async def regime_edge_preview(_user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.regime.regime_edge import preview_all
    return {"ok": True, **(await preview_all())}


@router.get("/setups")
async def regime_setups(status: str = "active", limit: int = 30,
                        _user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.setup_coalescer import COLLECTION
    q = {"status": status} if status != "all" else {}
    rows = await db[COLLECTION].find(
        q, {"_id": 0, "confidence_series": 0},
    ).sort("last_seen", -1).max_time_ms(5000).to_list(min(int(limit), 200))
    counts = {
        "active": await db[COLLECTION].count_documents({"status": "active"}),
        "terminated": await db[COLLECTION].count_documents({"status": "terminated"}),
    }
    return {"ok": True, "rows": rows, "counts": counts}


class RefreshBody(BaseModel):
    lane: Optional[str] = None
    retrain: bool = False


@router.post("/refresh")
async def regime_refresh(body: RefreshBody,
                         _user: dict = Depends(get_current_user)):  # noqa: B008
    from shared.regime.snapshot import run_now
    result = await run_now(body.lane, retrain=body.retrain)
    return {"ok": True, "result": result}
