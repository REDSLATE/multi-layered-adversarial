"""Runtime system flags — DB-backed feature toggles with sync read API.

Operator pin (2026-02-23): Flipping `PARADOX_V3_BRAINS`,
`PARADOX_V3_TRIGGER_WATCHER`, or `PARADOX_V3_TRIGGER_REFIRE` used to
require an env-var edit + backend restart — a deploy ceremony the
operator could not perform from the dashboard. After the user
reported "I don't see any way of changing env to camino" (PROD
mission.risedual.ai, 2026-02-23), this module moves the same flags
to a DB-backed `system_flags` doc that can be flipped from the UI.

Design constraints:

  * The read API (`get_system_flags()`) MUST stay synchronous. The
    brain runner calls `v3_brain_enabled()` from a sync code path on
    every intent emit — turning that async would ripple to every
    caller. Solution: a process-local cache, refreshed by an async
    background task every 5s. Sync reads return the cached snapshot.

  * Backwards-compatible: when the cache has never been hydrated
    (cold start, DB unreachable), the env-var behaviour kicks in
    so we never SILENTLY downgrade an env-configured rollout.

  * Cache is invalidated immediately by `refresh_system_flags()`
    after any admin POST, so an operator flip becomes effective in
    the next emit tick (typically <1s end-to-end).

  * Empty list `paradox_v3_brains: []` means "operator explicitly
    set no brains on v3" — DIFFERENT from `None` (cache cold,
    fall back to env). The sentinel is preserved through all read
    paths.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from db import db
from namespaces import SYSTEM_FLAGS, SYSTEM_FLAG_CHANGES

logger = logging.getLogger("risedual.system_flags")


# Singleton doc id in the SYSTEM_FLAGS collection. There is exactly
# one current-flag row; history lives in SYSTEM_FLAG_CHANGES.
_DOC_ID = "current"

# Soft TTL on the process-local cache. The background refresher
# polls every CACHE_TTL_SECONDS; any admin POST also force-refreshes
# the cache immediately so changes become visible without waiting.
CACHE_TTL_SECONDS = 5.0


@dataclass
class SystemFlagsSnapshot:
    """Immutable read-side view of the current flags.

    `None` on a flag means "DB has not been hydrated yet / row does
    not exist" — callers MUST fall back to env-var behaviour. A
    populated value (incl. empty list / False) means the operator
    has explicitly set the flag and DB is the source of truth.
    """
    paradox_v3_brains:        Optional[List[str]] = None
    trigger_watcher_enabled:  Optional[bool]      = None
    trigger_refire_enabled:   Optional[bool]      = None
    fetched_at:               float               = field(default_factory=lambda: 0.0)
    hydrated:                 bool                = False


# Module-local cache. The async refresher writes; sync readers read.
# Single-process FastAPI worker — no cross-process locking needed.
_cache: SystemFlagsSnapshot = SystemFlagsSnapshot()
_refresher_task: Optional[asyncio.Task] = None


def _parse_doc(doc: Optional[dict]) -> SystemFlagsSnapshot:
    """Coerce a `system_flags` Mongo doc into a SystemFlagsSnapshot."""
    if not doc:
        # Row missing entirely — treat as hydrated-but-empty so the
        # admin endpoints can write the first row, and so we don't
        # keep flipping back to env on every read.
        return SystemFlagsSnapshot(
            paradox_v3_brains=None,
            trigger_watcher_enabled=None,
            trigger_refire_enabled=None,
            fetched_at=time.monotonic(),
            hydrated=True,
        )
    brains = doc.get("paradox_v3_brains")
    if brains is not None and not isinstance(brains, list):
        brains = None
    if brains is not None:
        brains = sorted({str(b).strip().lower() for b in brains if str(b).strip()})
    watcher = doc.get("trigger_watcher_enabled")
    refire = doc.get("trigger_refire_enabled")
    return SystemFlagsSnapshot(
        paradox_v3_brains=brains,
        trigger_watcher_enabled=(bool(watcher) if watcher is not None else None),
        trigger_refire_enabled=(bool(refire) if refire is not None else None),
        fetched_at=time.monotonic(),
        hydrated=True,
    )


async def refresh_system_flags() -> SystemFlagsSnapshot:
    """Force a one-shot refresh of the cache from MongoDB.

    Called by the admin POST handlers immediately after writing so
    the new value is visible on the next sync read. Also called by
    the background refresher loop every CACHE_TTL_SECONDS.
    """
    global _cache
    try:
        doc = await db[SYSTEM_FLAGS].find_one({"_id": _DOC_ID})
        _cache = _parse_doc(doc)
        return _cache
    except Exception as e:  # noqa: BLE001
        # Mongo unavailable / collection missing. KEEP the previous
        # cache; do NOT regress to env behaviour mid-flight.
        logger.warning("refresh_system_flags failed: %s", e)
        return _cache


def get_system_flags() -> SystemFlagsSnapshot:
    """Sync snapshot read.

    Returns the cached state. Caller is expected to fall back to
    env vars when a field is `None` AND the cache is not hydrated.
    """
    return _cache


async def _background_refresher() -> None:
    """Refresh the cache every CACHE_TTL_SECONDS. Idempotent; the
    admin POST handlers also force-refresh so admin actions don't
    wait on this loop."""
    while True:
        try:
            await refresh_system_flags()
        except Exception as e:  # noqa: BLE001
            logger.warning("background refresher tick failed: %s", e)
        await asyncio.sleep(CACHE_TTL_SECONDS)


async def start_background_refresher() -> None:
    """Boot the refresher task once per process. Call from server
    startup."""
    global _refresher_task
    if _refresher_task is not None and not _refresher_task.done():
        return
    # Initial hydration before the loop kicks off, so the FIRST
    # sync read after boot doesn't fall back to env.
    await refresh_system_flags()
    _refresher_task = asyncio.create_task(_background_refresher())


# ── Mutation helpers (admin endpoints) ─────────────────────────────
async def set_paradox_v3_brains(
    brains: List[str],
    *,
    actor: str,
) -> SystemFlagsSnapshot:
    """Set the brains-on-v3 list. Empty list = no brains.

    Audit row written to SYSTEM_FLAG_CHANGES. Cache refreshed
    synchronously so the next read sees the new value.
    """
    cleaned = sorted({str(b).strip().lower() for b in (brains or []) if str(b).strip()})
    before = (await db[SYSTEM_FLAGS].find_one({"_id": _DOC_ID}) or {}).get("paradox_v3_brains")
    await db[SYSTEM_FLAGS].update_one(
        {"_id": _DOC_ID},
        {"$set": {
            "paradox_v3_brains": cleaned,
            "updated_at": datetime.now(timezone.utc),
            "updated_by": actor,
        }},
        upsert=True,
    )
    await db[SYSTEM_FLAG_CHANGES].insert_one({
        "flag":   "paradox_v3_brains",
        "before": (before if isinstance(before, list) else None),
        "after":  cleaned,
        "actor":  actor,
        "ts":     datetime.now(timezone.utc),
    })
    return await refresh_system_flags()


async def set_trigger_watcher(enabled: bool, *, actor: str) -> SystemFlagsSnapshot:
    cur = await db[SYSTEM_FLAGS].find_one({"_id": _DOC_ID}) or {}
    before = cur.get("trigger_watcher_enabled")
    await db[SYSTEM_FLAGS].update_one(
        {"_id": _DOC_ID},
        {"$set": {
            "trigger_watcher_enabled": bool(enabled),
            "updated_at": datetime.now(timezone.utc),
            "updated_by": actor,
        }},
        upsert=True,
    )
    await db[SYSTEM_FLAG_CHANGES].insert_one({
        "flag":   "trigger_watcher_enabled",
        "before": (bool(before) if before is not None else None),
        "after":  bool(enabled),
        "actor":  actor,
        "ts":     datetime.now(timezone.utc),
    })
    return await refresh_system_flags()


async def set_trigger_refire(enabled: bool, *, actor: str) -> SystemFlagsSnapshot:
    cur = await db[SYSTEM_FLAGS].find_one({"_id": _DOC_ID}) or {}
    before = cur.get("trigger_refire_enabled")
    await db[SYSTEM_FLAGS].update_one(
        {"_id": _DOC_ID},
        {"$set": {
            "trigger_refire_enabled": bool(enabled),
            "updated_at": datetime.now(timezone.utc),
            "updated_by": actor,
        }},
        upsert=True,
    )
    await db[SYSTEM_FLAG_CHANGES].insert_one({
        "flag":   "trigger_refire_enabled",
        "before": (bool(before) if before is not None else None),
        "after":  bool(enabled),
        "actor":  actor,
        "ts":     datetime.now(timezone.utc),
    })
    return await refresh_system_flags()


async def recent_flag_changes(limit: int = 20) -> list[dict]:
    """Most recent flag changes for the audit feed on the tile."""
    out: list[dict] = []
    cursor = db[SYSTEM_FLAG_CHANGES].find(
        {}, {"_id": 0},
    ).sort("ts", -1).limit(int(limit))
    async for row in cursor:
        ts = row.get("ts")
        if hasattr(ts, "isoformat"):
            row["ts"] = ts.isoformat()
        out.append(row)
    return out


# ── Helpers used by the v3 gate functions ───────────────────────
def _env_csv_to_list(env_var: str) -> list[str]:
    val = os.environ.get(env_var, "").strip().lower()
    if not val:
        return []
    return sorted({b.strip() for b in val.split(",") if b.strip()})


def _env_bool(env_var: str) -> bool:
    return os.environ.get(env_var, "0").strip().lower() in {"1", "true", "yes", "on"}


def effective_paradox_v3_brains() -> list[str]:
    """Source of truth for the brain runner. DB wins; env is fallback."""
    snap = get_system_flags()
    if snap.paradox_v3_brains is not None:
        return list(snap.paradox_v3_brains)
    return _env_csv_to_list("PARADOX_V3_BRAINS")


def effective_trigger_watcher_enabled() -> bool:
    """Source of truth for the trigger watcher loop. DB wins."""
    snap = get_system_flags()
    if snap.trigger_watcher_enabled is not None:
        return bool(snap.trigger_watcher_enabled)
    return _env_bool("PARADOX_V3_TRIGGER_WATCHER")


def effective_trigger_refire_enabled() -> bool:
    """Source of truth for trigger refire. DB wins."""
    snap = get_system_flags()
    if snap.trigger_refire_enabled is not None:
        return bool(snap.trigger_refire_enabled)
    return _env_bool("PARADOX_V3_TRIGGER_REFIRE")


__all__ = (
    "SystemFlagsSnapshot",
    "get_system_flags",
    "refresh_system_flags",
    "start_background_refresher",
    "set_paradox_v3_brains",
    "set_trigger_watcher",
    "set_trigger_refire",
    "recent_flag_changes",
    "effective_paradox_v3_brains",
    "effective_trigger_watcher_enabled",
    "effective_trigger_refire_enabled",
)
