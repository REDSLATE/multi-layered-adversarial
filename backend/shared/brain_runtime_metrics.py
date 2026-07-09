"""Cached per-brain runtime metrics — Atlas-cheap replacement for
the shared_intents scan in `/api/admin/runtime/{brain}/status`.

Doctrine (2026-07-09 operator directive):

    "Runtime status should stop querying shared_intents live. Move
     status to a tiny cached heartbeat/metrics document. Update it
     when intents are written. Then status reads one small doc, not
     the huge intent tape."

Schema of `brain_runtime_metrics` (one doc per brain, `_id=brain`):
    _id            : brain canonical name (camino | barracuda | hellcat | gto)
    latest_ts      : ISO8601 of the most recent intent ingest_ts
    latest_action  : "BUY" | "SELL" | "HOLD" | ...
    latest_symbol  : "NVDA" / "BTC/USD" / ...
    last_1h        : count of intents in the last 60 min (rolling)
    last_24h       : count of intents in the last 24 hours (rolling)
    by_action      : {"BUY": n, "SELL": n, "HOLD": n} (rolling 24h)
    lifetime_count : monotonic emission counter (only ever ++)
    updated_at     : ISO8601 of the last write to THIS doc
    first_seen_at  : ISO8601 when the doc was first upserted

Writer:
    * `bump_on_emit()` — called on every `shared_intents.insert_one`
      to update the latest_* fields and increment lifetime_count.
    * `refresh_windows()` — computes last_1h / last_24h / by_action
      via a bounded aggregate query. Callable on-demand by the
      status endpoint (cached read) or a background scheduler.

Reader:
    * `get_metrics(brain)` — one small `find_one({_id: brain})` call.
      O(1) index hit vs the multi-million-row scan the old status
      endpoint did.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from db import db
from namespaces import SHARED_INTENTS

logger = logging.getLogger("brain_runtime_metrics")

COLLECTION = "brain_runtime_metrics"

# Refresh cadence: skip window recompute when a fresh recompute
# already ran within this budget. Keeps the status endpoint sub-100ms
# even under polling load (dashboard polls every ~5s per brain).
_WINDOW_REFRESH_MAX_AGE_S = 30.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def bump_on_emit(
    brain: str,
    action: Optional[str],
    symbol: Optional[str],
    ingest_ts: str,
) -> None:
    """Update the per-brain metrics doc after `shared_intents.insert_one`.

    Best-effort — a failure here MUST NEVER block intent emission,
    hence the top-level try/except swallow. Windowed counts
    (last_1h/last_24h/by_action) are NOT touched here; they are
    recomputed on-demand by `refresh_windows()` at read time. This
    keeps the write path allocation-free and avoids a second Atlas
    round-trip per intent.
    """
    if not brain:
        return
    try:
        await db[COLLECTION].update_one(
            {"_id": brain},
            {
                "$set": {
                    "latest_ts": ingest_ts,
                    "latest_action": (action or "").upper() or None,
                    "latest_symbol": symbol,
                    "updated_at": _now_iso(),
                },
                "$inc": {"lifetime_count": 1},
                "$setOnInsert": {
                    "_id": brain,
                    "first_seen_at": _now_iso(),
                },
            },
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("brain_runtime_metrics.bump_on_emit failed: %s", exc)


async def refresh_windows(brain: str, *, force: bool = False) -> Optional[Dict[str, Any]]:
    """Recompute last_1h / last_24h / by_action for `brain` off
    `shared_intents`, bounded to the last 24h so the query planner
    always uses the `(stack_canonical, ingest_ts)` composite index.

    Cached: if the doc's `windows_refreshed_at` is younger than
    `_WINDOW_REFRESH_MAX_AGE_S`, we return the cached doc unchanged.
    Pass `force=True` from a maintenance script to bypass.

    Returns the fresh metrics doc (post-update), or None on Atlas failure.
    """
    now = _now()
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    cutoff_1h = (now - timedelta(hours=1)).isoformat()

    # Cheap short-circuit: cached refresh still valid?
    if not force:
        try:
            existing = await db[COLLECTION].find_one({"_id": brain})
            if existing and existing.get("windows_refreshed_at"):
                try:
                    refreshed = datetime.fromisoformat(
                        existing["windows_refreshed_at"].replace("Z", "+00:00"),
                    )
                    age = (now - refreshed).total_seconds()
                    if age < _WINDOW_REFRESH_MAX_AGE_S:
                        return existing
                except (ValueError, AttributeError):
                    pass
        except Exception:  # noqa: BLE001
            pass

    # Canonicalize the brain name for the intent filter — historical
    # docs carry `stack_canonical` since the 2026-02-23 migration.
    try:
        from shared.brain_legend import canonicalize_stack  # noqa: WPS433
        brain_c = canonicalize_stack(brain) or brain
    except Exception:  # noqa: BLE001
        brain_c = brain

    last_1h: Optional[int] = None
    last_24h: Optional[int] = None
    by_action: Dict[str, int] = {}

    try:
        cursor = db[SHARED_INTENTS].aggregate(
            [
                {"$match": {
                    "stack_canonical": brain_c,
                    "ingest_ts": {"$gte": cutoff_24h},
                }},
                {"$group": {
                    "_id": "$action",
                    "count": {"$sum": 1},
                    "recent_1h": {
                        "$sum": {
                            "$cond": [
                                {"$gte": ["$ingest_ts", cutoff_1h]},
                                1, 0,
                            ],
                        },
                    },
                }},
            ],
        )
        total_24h = 0
        total_1h = 0
        async for row in cursor:
            action_key = str(row.get("_id") or "UNK").upper()
            cnt_24h = int(row.get("count", 0))
            cnt_1h = int(row.get("recent_1h", 0))
            total_24h += cnt_24h
            total_1h += cnt_1h
            by_action[action_key] = cnt_24h
        last_24h = total_24h
        last_1h = total_1h
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "brain_runtime_metrics.refresh_windows aggregate failed "
            "brain=%s err=%s", brain, exc,
        )
        return None

    try:
        await db[COLLECTION].update_one(
            {"_id": brain},
            {
                "$set": {
                    "last_1h": last_1h,
                    "last_24h": last_24h,
                    "by_action": by_action,
                    "windows_refreshed_at": _now_iso(),
                    "updated_at": _now_iso(),
                },
                "$setOnInsert": {
                    "_id": brain,
                    "first_seen_at": _now_iso(),
                },
            },
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "brain_runtime_metrics.refresh_windows update failed "
            "brain=%s err=%s", brain, exc,
        )
        return None

    try:
        return await db[COLLECTION].find_one({"_id": brain})
    except Exception:  # noqa: BLE001
        return None


async def get_metrics(brain: str) -> Optional[Dict[str, Any]]:
    """Read the cached metrics doc. Returns None if the brain has
    never emitted (no doc exists yet)."""
    if not brain:
        return None
    try:
        return await db[COLLECTION].find_one({"_id": brain})
    except Exception as exc:  # noqa: BLE001
        logger.warning("brain_runtime_metrics.get_metrics failed: %s", exc)
        return None
