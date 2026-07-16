"""One-shot production data-nuke endpoint (2026-07-15).

Drops a hardcoded allowlist of TEST/AUDIT collections. Preserves
everything auth/credentials/config related.

Hit ONCE from the browser after logging in:
    POST https://mission.risedual.ai/api/admin/nuke-test-data?confirm=YES_I_MEAN_IT

Requires auth, requires the exact confirm string. Delete this file
after use — do NOT leave a data-nuke endpoint sitting in prod
long-term."""
from __future__ import annotations

import logging
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import get_current_user
from db import db

logger = logging.getLogger("risedual.nuke")
router = APIRouter(prefix="/admin", tags=["nuke"])

# Explicit allowlist. Everything NOT in this list is preserved.
DISPOSABLE_COLLECTIONS = [
    "mc_pulse_receipts",
    "mc_opinions",
    "mc_opinions_compare",
    "shared_intents",
    "shared_ohlcv_bars",
    "shared_labeled_memory",
    "shared_adl_receipts",
    "executions",
    "execution_receipts",
    "universe_refresh_reports",
    "symbol_registry",
    "live_universe",
    "login_attempts",
    "daily_market_snapshots",
]

# Explicit protected list — if we ever accidentally add one of
# these to DISPOSABLE_COLLECTIONS the code refuses to drop it.
PROTECTED_COLLECTIONS = frozenset([
    "users",
    "kraken_credentials",
    "webull_token",
    "webull_credentials",
    "mc_seats",
    "patterns_universe",
])


@router.post("/nuke-test-data")
async def nuke_test_data(
    confirm: str = Query(..., description="Must be exactly YES_I_MEAN_IT"),
    _user=Depends(get_current_user),
) -> dict:
    if confirm != "YES_I_MEAN_IT":
        raise HTTPException(400, "confirm param must be YES_I_MEAN_IT")

    results: dict[str, dict] = {}
    for coll in DISPOSABLE_COLLECTIONS:
        if coll in PROTECTED_COLLECTIONS:
            results[coll] = {"skipped": "protected"}
            continue
        try:
            before = await db[coll].estimated_document_count()
        except Exception as exc:  # noqa: BLE001
            before = f"count_error: {exc}"
        try:
            await db[coll].drop()
            results[coll] = {"before": before, "dropped": True}
        except Exception as exc:  # noqa: BLE001
            results[coll] = {"before": before, "dropped": False, "err": str(exc)}
    logger.warning("nuke_test_data ran: %s", results)
    return {"ok": True, "results": results}
