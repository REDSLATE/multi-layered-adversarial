"""Feeder health audit — central rolling log of provider errors/429s.

Every feeder writes one row here on any HTTP error or rate-limit
event. The `/api/admin/feeders/health-audit` endpoint reads this
collection so the operator can see at a glance which providers are
behaving and which are throttling.

Doctrine: this collection is descriptive — failed fetches NEVER block
ingest. The pipeline degrades gracefully; the audit row is the
forensic trail. RoadGuard and the gate chain never read this table.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from db import db
from namespaces import FEEDER_HEALTH_AUDIT


# Per-provider rolling window cap so this collection cannot grow
# unbounded. The most recent N rows per provider are always kept.
ROLLING_CAP_PER_PROVIDER = 500


async def record_feeder_health(
    *,
    provider: str,
    endpoint: str,
    status_code: Optional[int],
    error_type: str,
    message: str,
    context: Optional[dict[str, Any]] = None,
) -> None:
    """Insert one feeder-health row. Best-effort — failures in the
    audit path must never crash the calling worker."""
    doc = {
        "provider": provider,
        "endpoint": endpoint,
        "status_code": status_code,
        "error_type": error_type,  # http_status_error | request_error | api_error | db_error | rate_limit
        "message": (message or "")[:2000],
        "context": context or {},
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        await db[FEEDER_HEALTH_AUDIT].insert_one(doc)
        # Trim — keep the latest ROLLING_CAP_PER_PROVIDER rows per provider.
        total = await db[FEEDER_HEALTH_AUDIT].count_documents({"provider": provider})
        if total > ROLLING_CAP_PER_PROVIDER:
            # Find the cutoff ts (oldest row to KEEP).
            cutoff = await db[FEEDER_HEALTH_AUDIT].find(
                {"provider": provider}, {"_id": 0, "ts": 1},
            ).sort("ts", -1).skip(ROLLING_CAP_PER_PROVIDER).limit(1).to_list(1)
            if cutoff:
                await db[FEEDER_HEALTH_AUDIT].delete_many(
                    {"provider": provider, "ts": {"$lt": cutoff[0]["ts"]}},
                )
    except Exception:  # noqa: BLE001 — audit must never crash callers
        pass
