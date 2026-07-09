"""Cached per-brain runtime metrics — Atlas-cheap replacement for
the shared_intents scan in `/api/admin/runtime/{brain}/status`.

Doctrine (2026-07-09 operator directive):

    "Runtime status should stop querying shared_intents live. Move
     status to a tiny cached heartbeat/metrics document. Update it
     when intents are written. Then status reads one small doc, not
     the huge intent tape."

Schema of `brain_runtime_metrics`:
    _id           : brain canonical name (camino | barracuda | hellcat | gto)
    latest_ts     : ISO8601 of the most recent intent ingest_ts
    latest_action : "BUY" | "SELL" | "HOLD" | ...
    latest_symbol : "NVDA" / "BTC/USD" / ...
    last_1h       : count of intents in the last 60 min (updated by cron)
    last_24h      : count of intents in the last 24 hours (updated by cron)
    by_action     : {"BUY": n, "SELL": n, "HOLD": n} (rolling 24h)
    updated_at    : ISO8601 of the last write to THIS doc

Writer:
    * `bump_on_emit()` — called on every `shared_intents.insert_one`
      to update the latest_* fields and increment last_1h/last_24h.
    * Counts are best-effort. If they drift by ±1 during high-load
      or a restart, they self-heal on the next scheduled refresh.

Reader:
    * `get_metrics(brain)` — one small `find_one({_id: brain})` call.
      O(1) index hit vs the multi-million-row scan the old status
      endpoint did.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from db import db

logger = logging.getLogger("brain_runtime_metrics")

COLLECTION = "brain_runtime_metrics"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def bump_on_emit(
    brain: str, action: str, symbol: Optional[str], ingest_ts: str,
) -> None:
    """Update the per-brain metrics doc after `shared_intents.insert_one`.

    Best-effort — a failure here MUST NEVER block intent emission,
    hence the top-level try/except swallow. Metrics can be rebuilt
    from `shared_intents` if they drift.
    """
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
                "$inc": {
                    "lifetime_count": 1,
                    f"by_action.{(action or 'UNK').upper()}": 1,
                },
                "$setOnInsert": {"_id": brain, "first_seen_at": _now_iso()},
            },
            upsert=True,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("brain_runtime_metrics.bump_on_emit failed: %s", exc)


async def get_metrics(brain: str) -> Optional[Dict[str, Any]]:
    """Read the cached metrics doc. Returns None if the brain has
    never emitted (no doc exists yet)."""
    try:
        return await db[COLLECTION].find_one({"_id": brain}, {"_id": 0})
    except Exception as exc:  # noqa: BLE001
        logger.warning("brain_runtime_metrics.get_metrics failed: %s", exc)
        return None
