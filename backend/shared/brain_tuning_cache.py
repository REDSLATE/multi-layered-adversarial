"""Brain tuning override — operator-flippable per-lane thresholds.

When intents are HOLD-heavy and the operator says "less conservative",
the lever is brain-level: `min_gap` (how much directional confidence
beats HOLD) and `min_confidence` (minimum commitment to fire at all).

This module is a thin in-process cache + Mongo singleton. The brain
runner refreshes the cache every 30s on a background task; brain_core
reads from the cache synchronously during `evaluate()` so we don't
hit Mongo per opinion.

Storage:
    db.runtime_flags._id = "brain_tuning"
    {
      "_id": "brain_tuning",
      "overrides": {
        "equity": {"min_gap": 0.04, "min_confidence": 0.50, "hold_spread_coef": 0.002},
        "crypto": {"min_gap": 0.025, "min_confidence": 0.55, "hold_spread_coef": 0.0008}
      },
      "updated_at": "...", "updated_by": "..."
    }

`None` / missing fields fall back to the brain's compiled defaults.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional


logger = logging.getLogger("brain_tuning_cache")

_FLAG_ID = "brain_tuning"
_CACHE: dict[str, dict] = {}  # lane → {min_gap?, min_confidence?, hold_spread_coef?}
_CACHE_TS: float = 0.0
_CACHE_TTL_SEC: float = 30.0
_REFRESH_TASK: Optional[asyncio.Task] = None


def get_override(lane: str, key: str) -> Optional[float]:
    """Read-only synchronous lookup for brain_core.

    Returns the operator override value for `(lane, key)` if set; None
    if the operator hasn't overridden this knob — caller should fall
    back to its compiled default. Never raises; treats a missing
    cache as "no override".
    """
    if not _CACHE:
        return None
    lane_doc = _CACHE.get(lane) or {}
    val = lane_doc.get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


async def refresh_cache() -> dict:
    """Pull the override doc from Mongo into the module-level cache.
    Called by the background refresher AND by the admin POST so the
    cache is coherent with the operator's last action."""
    global _CACHE_TS
    from db import db  # noqa: WPS433
    try:
        doc = await db["runtime_flags"].find_one(
            {"_id": _FLAG_ID}, {"_id": 0},
        )
        overrides = (doc or {}).get("overrides") or {}
        # Replace the entire cache so deleted-keys actually deleted.
        _CACHE.clear()
        _CACHE.update({
            lane: dict(v or {}) for lane, v in overrides.items()
        })
        _CACHE_TS = time.time()
        return {"cached_lanes": list(_CACHE.keys()), "ts": _CACHE_TS}
    except Exception as e:  # noqa: BLE001
        logger.warning("brain_tuning cache refresh failed: %s", e)
        return {"cached_lanes": [], "error": str(e)}


async def _refresher_loop() -> None:
    """Long-running refresher. Pulls the override doc every 30s so
    the brains see the operator's last UI flip within one cycle."""
    while True:
        try:
            await refresh_cache()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("brain_tuning refresher tick failed: %s", e)
        await asyncio.sleep(_CACHE_TTL_SEC)


def start_refresher_if_needed() -> None:
    global _REFRESH_TASK
    if _REFRESH_TASK and not _REFRESH_TASK.done():
        return
    try:
        loop = asyncio.get_event_loop()
        _REFRESH_TASK = loop.create_task(_refresher_loop())
        logger.info("brain_tuning cache refresher started (TTL=%ss)", _CACHE_TTL_SEC)
    except Exception as e:  # noqa: BLE001
        logger.warning("brain_tuning refresher start failed: %s", e)


async def stop_refresher() -> None:
    global _REFRESH_TASK
    if _REFRESH_TASK and not _REFRESH_TASK.done():
        _REFRESH_TASK.cancel()
        try:
            await _REFRESH_TASK
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _REFRESH_TASK = None
