"""Admin control for the unified execution pipeline feature flag.

The flag has two enable paths (env var OR mongo). This module
manages the mongo path so the operator can flip the pipeline on/off
from the admin UI without touching deploy env vars.

Endpoints:
    POST /api/admin/unified-pipeline/start   — flip flag ON
    POST /api/admin/unified-pipeline/stop    — flip flag OFF
    GET  /api/admin/unified-pipeline/status  — current state + last flip metadata
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db
from shared.pipeline.adapter import refresh_pipeline_flag_cache


router = APIRouter(prefix="/admin/unified-pipeline", tags=["unified-pipeline-admin"])


_FLAG_DOC_ID = "unified_pipeline_enabled"
_COLL = "runtime_flags"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/status")
async def status(_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Return the current pipeline-flag state from both sources so the
    operator can see exactly why the pipeline is on or off.
    """
    doc = await db[_COLL].find_one({"_id": _FLAG_DOC_ID}, {"_id": 0}) or {}
    env_val = os.environ.get("UNIFIED_PIPELINE_ENABLED", "false").lower()
    env_enabled = env_val == "true"
    mongo_enabled = bool(doc.get("enabled", False))
    return {
        "effective_enabled": env_enabled or mongo_enabled,
        "sources": {
            "env": {"set": env_val != "false", "value": env_val, "enabled": env_enabled},
            "mongo": {
                "enabled": mongo_enabled,
                "updated_at": doc.get("updated_at"),
                "updated_by": doc.get("updated_by"),
                "last_reason": doc.get("reason"),
            },
        },
        "note": (
            "Either source flips the pipeline on. Env is set in your "
            "deploy config; mongo flag is flipped via POST start/stop. "
            "When env is true, the mongo flag is ignored."
        ),
    }


@router.post("/start")
async def start(user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Flip the mongo flag to True so the next auto-router tick uses
    the unified pipeline. Effect is near-instant (5-second flag cache).
    """
    now = _now()
    await db[_COLL].update_one(
        {"_id": _FLAG_DOC_ID},
        {"$set": {
            "enabled": True,
            "updated_at": now,
            "updated_by": user.get("email") or "operator",
            "reason": "started via /admin/unified-pipeline/start",
        }},
        upsert=True,
    )
    fresh = await refresh_pipeline_flag_cache()
    return {
        "ok": True,
        "effective_enabled": fresh or os.environ.get(
            "UNIFIED_PIPELINE_ENABLED", "false",
        ).lower() == "true",
        "flipped_at": now,
        "next_tick_uses_pipeline": True,
    }


@router.post("/stop")
async def stop(user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Flip the mongo flag to False. If env var is also set, the
    pipeline stays on — the env var wins. The response makes that
    explicit so the operator doesn't waste time wondering.
    """
    now = _now()
    await db[_COLL].update_one(
        {"_id": _FLAG_DOC_ID},
        {"$set": {
            "enabled": False,
            "updated_at": now,
            "updated_by": user.get("email") or "operator",
            "reason": "stopped via /admin/unified-pipeline/stop",
        }},
        upsert=True,
    )
    fresh = await refresh_pipeline_flag_cache()
    env_on = os.environ.get("UNIFIED_PIPELINE_ENABLED", "false").lower() == "true"
    effective = fresh or env_on
    return {
        "ok": True,
        "effective_enabled": effective,
        "flipped_at": now,
        "warning": (
            "env var UNIFIED_PIPELINE_ENABLED=true is still set in your "
            "deploy config — pipeline remains ON until you unset it there."
            if env_on else None
        ),
    }
