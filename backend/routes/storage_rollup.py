"""Operator-facing storage rollup endpoints.

  GET  /api/admin/storage-rollup/preview      Phase 1 dry-run
  POST /api/admin/storage-rollup/run          Phase 1 (rollup) live
  GET  /api/admin/storage-rollup/purge-preview Phase 2 dry-run
  POST /api/admin/storage-rollup/purge        Phase 2 (delete) live
  GET  /api/admin/storage-rollup/stats        Per-collection sizes +
                                              rollup coverage

All endpoints require admin JWT (`get_current_user`)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db
from shared.storage_rollup.config import (
    PROTECTED_COLLECTIONS,
    ROLLUP_DELETE_HOLD_DAYS,
    ROLLUP_WINDOW_DAYS,
)
from shared.storage_rollup.registry import ROLLUP_COLLECTIONS
from shared.storage_rollup.runner import (
    purge_collection,
    rollup_collection,
)


router = APIRouter(prefix="/admin/storage-rollup", tags=["storage-rollup"])


async def _run_all(*, dry_run: bool) -> dict:
    results = []
    total_rolled = 0
    total_scanned = 0
    for entry in ROLLUP_COLLECTIONS:
        r = await rollup_collection(
            entry["name"], entry["ts_field"], dry_run=dry_run,
        )
        results.append(r)
        total_rolled += r.get("rolled", 0)
        total_scanned += r.get("scanned", 0)
    return {
        "ok": True,
        "dry_run": dry_run,
        "window_days": ROLLUP_WINDOW_DAYS,
        "totals": {"scanned": total_scanned, "rolled": total_rolled},
        "results": results,
    }


async def _purge_all(*, dry_run: bool) -> dict:
    results = []
    total_purged = 0
    total_scanned = 0
    for entry in ROLLUP_COLLECTIONS:
        r = await purge_collection(entry["name"], dry_run=dry_run)
        results.append(r)
        total_purged += r.get("purged", 0)
        total_scanned += r.get("scanned", 0)
    return {
        "ok": True,
        "dry_run": dry_run,
        "hold_days": ROLLUP_DELETE_HOLD_DAYS,
        "totals": {"scanned": total_scanned, "purged": total_purged},
        "results": results,
    }


@router.get("/preview")
async def preview(_user: dict = Depends(get_current_user)) -> dict:  # noqa: B008
    return await _run_all(dry_run=True)


@router.post("/run")
async def run(_user: dict = Depends(get_current_user)) -> dict:  # noqa: B008
    return await _run_all(dry_run=False)


@router.get("/purge-preview")
async def purge_preview(_user: dict = Depends(get_current_user)) -> dict:  # noqa: B008
    return await _purge_all(dry_run=True)


@router.post("/purge")
async def purge(_user: dict = Depends(get_current_user)) -> dict:  # noqa: B008
    return await _purge_all(dry_run=False)


@router.get("/stats")
async def stats(_user: dict = Depends(get_current_user)) -> dict:  # noqa: B008
    """Per-collection size + rollup coverage. Lets the operator see
    where storage actually lives + how much has already been compressed."""
    out = []
    for entry in ROLLUP_COLLECTIONS:
        name = entry["name"]
        is_present = bool(
            await db.list_collection_names(filter={"name": name})
        )
        if not is_present:
            out.append({
                "collection": name,
                "present": False,
            })
            continue
        try:
            s = await db.command("collStats", name, scale=1024 * 1024)
        except Exception:  # noqa: BLE001
            s = {}
        total = await db[name].count_documents({})
        rolled = await db[name].count_documents(
            {"rolled_up_at": {"$exists": True}}
        )
        rollups_count = 0
        rollups_name = f"{name}_rollups"
        if await db.list_collection_names(filter={"name": rollups_name}):
            rollups_count = await db[rollups_name].count_documents({})
        out.append({
            "collection": name,
            "present": True,
            "ts_field": entry["ts_field"],
            "data_mb": round(s.get("size", 0), 3),
            "storage_mb": round(s.get("storageSize", 0), 3),
            "index_mb": round(s.get("totalIndexSize", 0), 3),
            "doc_count": total,
            "rolled_up_count": rolled,
            "rollups_collection_count": rollups_count,
            "rolled_pct": round((rolled / total) * 100, 2) if total else 0.0,
        })
    return {
        "ok": True,
        "window_days": ROLLUP_WINDOW_DAYS,
        "hold_days": ROLLUP_DELETE_HOLD_DAYS,
        "protected_collections": sorted(PROTECTED_COLLECTIONS),
        "rows": out,
    }
