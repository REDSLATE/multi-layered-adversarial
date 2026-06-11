"""Data Council status — who's primary, who's council-of-last-resort.

Operator doctrine (2026-06-11):
    Webull (equity) and Kraken (crypto) are now the PRIMARY data
    sources for the brain hot loop. Polygon and Finnhub remain alive
    inside the runtime but are demoted to "council-of-last-resort" —
    consulted only when the primary source can't carry the field.

This endpoint surfaces:
    * Which sources the brain currently treats as primary per lane
    * Which sources are still in the council
    * Live status of each (entitled / configured / failing)
    * Recent feeder-health audit row counts so the operator can spot
      a council member drowning in 429s without it dragging the tick

No code execution decisions are made here — it's pure observation.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db

logger = logging.getLogger("risedual.data_council")
router = APIRouter(prefix="/admin/data-council", tags=["data-council"])


def _env_flag(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@router.get("/status")
async def get_council_status(_user: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Return the data-council state for both lanes."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()

    # Feeder-health audit counts over the last 15 min (the feeders
    # write a row on every poll: ok / error / 429 / configuration).
    feeder_rows = await db["feeder_health_audit"].aggregate([
        {"$match": {"ts": {"$gte": cutoff}}},
        {"$group": {
            "_id": {"feeder": "$feeder", "status": "$status"},
            "count": {"$sum": 1},
        }},
    ]).to_list(200)
    feeder_summary: Dict[str, Dict[str, int]] = {}
    for row in feeder_rows:
        key = row["_id"]
        feeder = key.get("feeder") or "unknown"
        status = key.get("status") or "unknown"
        feeder_summary.setdefault(feeder, {})[status] = row.get("count", 0)

    # Webull entitlement state — already cached in the quotes client
    webull_ents = {"us_stock_quotes": False, "us_crypto": False}
    try:
        from shared.market_data.webull_quotes import get_quotes_client  # noqa: WPS433
        client = get_quotes_client()
        if client is not None:
            ent = client.get_entitlements()
            webull_ents = ent.get("data_classes") or webull_ents
    except Exception:  # noqa: BLE001
        pass

    polygon_configured = bool((os.environ.get("POLYGON_API_KEY") or "").strip())
    polygon_enabled = _env_flag("POLYGON_FEEDER_ENABLED", default=polygon_configured)
    finnhub_configured = bool((os.environ.get("FINNHUB_API_KEY") or "").strip())
    finnhub_enabled = _env_flag("FINNHUB_ENABLED", default=finnhub_configured)
    kraken_configured = await db["kraken_credentials"].count_documents({}) > 0

    return {
        "lanes": {
            "equity": {
                "primary": {
                    "name": "webull",
                    "status": "live" if webull_ents.get("us_stock_quotes") else "gated",
                    "entitlement": "us_stock_quotes",
                },
                "council": [
                    {
                        "name": "polygon",
                        "status": "live" if polygon_enabled and polygon_configured else "off",
                        "configured": polygon_configured,
                        "enabled": polygon_enabled,
                        "feeder_health": feeder_summary.get("polygon_equity", {}),
                        "role": "council_of_last_resort",
                    },
                    {
                        "name": "finnhub",
                        "status": "live" if finnhub_enabled and finnhub_configured else "off",
                        "configured": finnhub_configured,
                        "enabled": finnhub_enabled,
                        "feeder_health": feeder_summary.get("finnhub_equity", {}),
                        "role": "council_of_last_resort",
                    },
                ],
            },
            "crypto": {
                "primary": {
                    "name": "kraken",
                    "status": "live" if kraken_configured else "no_credentials",
                },
                "council": [
                    {
                        "name": "webull",
                        "status": "live" if webull_ents.get("us_crypto") else "gated",
                        "role": "cross_check",
                    },
                ],
            },
        },
        "doctrine": (
            "Webull is the primary equity feed; Kraken is the primary "
            "crypto feed. Polygon and Finnhub are kept in the council "
            "but consulted only when the primary source can't carry "
            "the field. Per-call technical-fetch timeout is 3s so a "
            "slow council member cannot drag the brain tick budget."
        ),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
