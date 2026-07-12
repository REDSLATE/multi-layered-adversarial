"""Meta endpoints — `/`, `/health`, `/admin/neutral-brains/status`.

Extracted from server.py on 2026-06-18. Behavior 1:1.

These three endpoints used to be inline `@api_router.get(...)`
definitions in server.py. They're moved here both to keep server.py
thin AND to keep them adjacent to each other in source — all three
report runtime/deployment posture; they belong together.
"""
from __future__ import annotations

import os

from fastapi import APIRouter

from db import client


router = APIRouter()


@router.get("/admin/neutral-brains/status")
async def neutral_brains_status():
    """Post-P3-step-3: the 4 in-process neutral brain runners were
    retired in favor of the MC Pulse loop. This endpoint returns a
    stable schema (kept for dashboard backward-compat) reporting the
    canonical 4-brain roster with `enabled=False` and an empty
    `runners` list. The current dashboard should read pulse activity
    via `GET /api/mc/parity/{brain}` or `GET /api/mc/arbiter/state`
    instead.
    """
    # Static roster — the brand mapping doesn't change post-migration.
    static_roster = [
        {"brain_id": "camino",    "display_name": "Camino",
         "token_env": "CAMINO_INGEST_TOKEN",    "legacy_token_env": "ALPHA_INGEST_TOKEN"},
        {"brain_id": "barracuda", "display_name": "Barracuda",
         "token_env": "BARRACUDA_INGEST_TOKEN", "legacy_token_env": "CAMARO_INGEST_TOKEN"},
        {"brain_id": "hellcat",   "display_name": "Hellcat",
         "token_env": "HELLCAT_INGEST_TOKEN",   "legacy_token_env": "CHEVELLE_INGEST_TOKEN"},
        {"brain_id": "gto",       "display_name": "GTO",
         "token_env": "GTO_INGEST_TOKEN",       "legacy_token_env": "REDEYE_INGEST_TOKEN"},
    ]
    return {
        "enabled": False,
        "runners": [],
        "roster": static_roster,
        "note": "legacy runners deleted 2026-07-12 (P3 step 3); pulse is the sole brain path",
    }


@router.get("/")
async def root():
    return {
        "name": "RISEDUAL Mission Control",
        "deploy_mode": os.environ.get("DEPLOY_MODE", "observation"),
        "runtimes": ["camino", "barracuda", "hellcat"],
        "doctrine": "one shared nervous system, three separate decision brains",
    }


@router.get("/health")
async def health():
    try:
        await client.admin.command("ping")
        mongo_ok = True
    except Exception:  # noqa: BLE001
        mongo_ok = False
    # Doctrine (2026-05-18 rev): deploy_mode reports OBSERVABLE STATE
    # based on what the broker ADAPTERS can actually do, not on a
    # DB-side `execution_enabled` flag (which is decorative — the
    # adapters never read it). If a broker adapter can be constructed
    # from current credentials, that's live trading capability.
    env_mode = os.environ.get("DEPLOY_MODE", "observation").lower()
    derived_mode = "observation"
    if mongo_ok:
        try:
            # Crypto: a Kraken adapter loads iff valid credentials are
            # present + decrypt cleanly.
            from shared.crypto.broker_adapter import get_kraken_adapter  # noqa: WPS433
            kraken_adapter = await get_kraken_adapter()
            # Equity: Webull adapter loads iff env vars are armed.
            from shared.broker.webull import get_webull_adapter  # noqa: WPS433
            equity_adapter = await get_webull_adapter()
            if kraken_adapter is not None or equity_adapter is not None:
                derived_mode = "execution"
        except Exception:  # noqa: BLE001
            pass
    # If either source says "execution", report execution.
    deploy_mode = "execution" if env_mode == "execution" or derived_mode == "execution" else "observation"
    return {
        "ok": True,
        "mongo": mongo_ok,
        "deploy_mode": deploy_mode,
        "deploy_mode_env": env_mode,
        "deploy_mode_derived": derived_mode,
    }
