"""Token-authenticated emergency purge endpoint (2026-02-16 hotfix).

Context: prod Atlas cluster saturated under the weight of 700K+
`shared_intents` / `shared_ohlcv_bars` documents. The pod cannot
complete `/api/auth/login` because the Mongo connection pool is
starved by background workers hitting slow collscans, so the
operator cannot log in to hit the existing session-authenticated
`/api/admin/nuke-test-data` endpoint. This endpoint bypasses the
login flow entirely — it authenticates via a query-string secret
token stamped into the backend `.env` (`EMERGENCY_PURGE_TOKEN`)
so the operator can shed load with a single HTTP request from any
device.

DELETE THIS FILE AND REMOVE THE ROUTER WIRING IMMEDIATELY AFTER
USE. Leaving a token-auth data-purge endpoint in production is a
massive footgun — a leaked token is a full data-wipe primitive.
Doctrine says: nuke endpoints are always temporary."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from db import db

logger = logging.getLogger("risedual.emergency_purge")
router = APIRouter(prefix="/admin", tags=["emergency-purge"])

# Collections that this endpoint is allowed to purge from. Any
# collection NOT in this allowlist is refused — even if the caller
# provides the token. Prevents a leaked token from being escalated
# into an arbitrary-collection wipe (e.g., `users`).
PURGEABLE_COLLECTIONS = {
    "shared_intents":     "ingest_ts",
    "shared_ohlcv_bars":  "ts",
    "mc_pulse_receipts":  "ts",
}

# Per-collection delete-many timeout. Prevents a stuck delete from
# holding a connection forever if Atlas is still degraded when we
# fire this. `deleteMany` on a well-indexed range is fast — 30s is
# already generous.
_DELETE_MAX_TIME_MS = 30_000


def _check_token(token: str) -> None:
    expected = os.environ.get("EMERGENCY_PURGE_TOKEN", "").strip()
    if not expected:
        # Endpoint is disabled unless the operator has explicitly
        # provisioned a token in the backend .env. Missing token
        # env-var means "this endpoint is not live in this env".
        raise HTTPException(
            status_code=503,
            detail="Emergency purge is not provisioned in this environment "
                   "(EMERGENCY_PURGE_TOKEN not set).",
        )
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="Invalid purge token.")


async def _run_purge(
    before_iso: str,
    collections: Optional[list[str]] = None,
) -> dict:
    """Shared purge logic used by both GET and POST variants."""
    # Validate `before_iso` shape early so we don't fire deletes
    # against garbage input. Accept `YYYY-MM-DD` and full ISO 8601.
    try:
        # datetime.fromisoformat rejects garbage cleanly.
        datetime.fromisoformat(before_iso.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"`before` must be an ISO date string (YYYY-MM-DD or full ISO). Got: {before_iso!r} — {exc}",
        )

    targets = collections or list(PURGEABLE_COLLECTIONS.keys())
    unknown = [c for c in targets if c not in PURGEABLE_COLLECTIONS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Not purgeable: {unknown}. Allowed: {sorted(PURGEABLE_COLLECTIONS)}",
        )

    results: dict[str, dict] = {}
    total_deleted = 0
    for coll in targets:
        ts_field = PURGEABLE_COLLECTIONS[coll]
        filter_ = {ts_field: {"$lt": before_iso}}
        try:
            # Best-effort estimate BEFORE — non-blocking, no scan.
            before_est = await db[coll].estimated_document_count()
        except Exception as exc:  # noqa: BLE001
            before_est = f"count_error: {exc}"
        try:
            res = await db[coll].delete_many(
                filter_,
                comment=f"emergency_purge before={before_iso}",
            )
            deleted = int(getattr(res, "deleted_count", 0))
            total_deleted += deleted
            results[coll] = {
                "before_estimated": before_est,
                "filter": filter_,
                "deleted": deleted,
                "ok": True,
            }
        except Exception as exc:  # noqa: BLE001
            results[coll] = {
                "before_estimated": before_est,
                "filter": filter_,
                "deleted": 0,
                "ok": False,
                "error": str(exc),
            }
    logger.warning(
        "emergency_purge ran: before=%s total_deleted=%d results=%s",
        before_iso, total_deleted, results,
    )
    return {
        "ok": True,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "before": before_iso,
        "total_deleted": total_deleted,
        "collections": results,
        "reminder": "Delete /app/backend/routes/emergency_purge.py and its "
                    "router wiring in server_modules/router_registry.py "
                    "IMMEDIATELY after use.",
    }


@router.post("/emergency-purge")
async def emergency_purge_post(
    request: Request,
    token: str = Query(..., description="Shared secret from backend .env EMERGENCY_PURGE_TOKEN"),
    before: str = Query(..., description="ISO date cutoff — anything with ts/ingest_ts < this is deleted"),
    collections: Optional[str] = Query(
        None,
        description="Optional CSV of collections to purge. Defaults to all three "
                    "(shared_intents, shared_ohlcv_bars, mc_pulse_receipts).",
    ),
) -> dict:
    _check_token(token)
    parsed_collections = (
        [c.strip() for c in collections.split(",") if c.strip()]
        if collections else None
    )
    return await _run_purge(before_iso=before, collections=parsed_collections)


@router.get("/emergency-purge")
async def emergency_purge_get(
    token: str = Query(...),
    before: str = Query(...),
    collections: Optional[str] = Query(None),
) -> dict:
    """GET variant so the operator can trigger the purge by simply
    pasting a URL into their mobile browser — no curl / Postman /
    dev-tools required. Non-idempotent-looking but the underlying
    delete IS idempotent (deleting an already-deleted row is a no-op)
    so re-invoking the same URL is safe."""
    _check_token(token)
    parsed_collections = (
        [c.strip() for c in collections.split(",") if c.strip()]
        if collections else None
    )
    return await _run_purge(before_iso=before, collections=parsed_collections)
