"""Trader admin routes — operator visibility for the sidecar trader.

Doctrine pin (2026-07-01, Path 3):
    Reads come from the LOCAL trader store (SQLite + in-memory
    caches), not Mongo. This is what keeps the dashboard alive when
    Atlas is degraded.

    Mongo still receives every row via the best-effort mirror
    worker — but the dashboard doesn't wait for it.

Endpoints:
    GET  /api/admin/trader/status         — task alive? last cycle? env?
    GET  /api/admin/trader/health         — store counts + mirror lag
    GET  /api/admin/trader/receipts       — last N receipts (local SQLite)
    GET  /api/admin/trader/executions     — last N executions (local SQLite)
    POST /api/admin/trader/reload-caches  — pokes the state refresher
    POST /api/admin/trader/seed-seats     — writes the operator's canonical
                                            angel-name + brain pairings into
                                            seat_registry (Mongo). Safe to
                                            call multiple times.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
import asyncio

from auth import get_current_user
from db import db


logger = logging.getLogger("risedual.admin.trader")
router = APIRouter(prefix="/admin/trader", tags=["admin", "trader"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_prefix() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _import_trader():
    """Lazy import — the trader package lives at /app/trader. Cheap
    once cached by the interpreter."""
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from trader import state, store   # noqa: WPS433
    return state, store


@router.get("/status")
async def trader_status(_: dict = Depends(get_current_user)) -> dict:
    """Operator visibility: is the trader alive and ticking?
    Reads exclusively from local SQLite + in-memory state — this
    endpoint MUST keep serving even when Atlas is unreachable."""
    enabled = os.environ.get("TRADER_ENABLED", "false").lower() == "true"
    broker_disabled = os.environ.get("BROKER_DISABLED", "false").lower() == "true"
    auto_router_off = os.environ.get("AUTO_ROUTER_ENABLED", "true").lower() == "false"

    state, store = _import_trader()

    # Last receipt = proxy for "loop is ticking".
    recent = store.recent_receipts(limit=1)
    last_receipt = recent[0] if recent else None

    # Receipt count in the last 5 minutes — sanity check the loop
    # is firing on schedule. Done via a lightweight SQLite COUNT.
    five_min_iso = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - 300, tz=timezone.utc,
    ).isoformat()
    all_recent = store.recent_receipts(limit=500)
    recent_count = sum(1 for r in all_recent if (r.get("ts") or "") >= five_min_iso)

    last_exec_rows = store.recent_executions(limit=1)
    last_execution = last_exec_rows[0] if last_exec_rows else None

    today = _today_prefix()
    fires_today_rows = store.recent_executions(limit=500, ok=True)
    fires_today = sum(1 for r in fires_today_rows
                      if (r.get("ts") or "").startswith(today))
    spent_today = sum(
        float(r.get("notional_usd") or 0.0)
        for r in fires_today_rows
        if (r.get("ts") or "").startswith(today)
    )

    return {
        "ok": True,
        "env": {
            "TRADER_ENABLED": enabled,
            "BROKER_DISABLED": broker_disabled,
            "AUTO_ROUTER_DISABLED": auto_router_off,
            "interval_sec": int(os.environ.get("TRADER_INTERVAL_SEC", "60")),
            "per_order_cap_usd": float(os.environ.get("TRADER_PER_ORDER_USD_CAP", "10")),
            "daily_cap_usd": float(os.environ.get("TRADER_DAILY_USD_CAP", "1000")),
            "crypto_pair": os.environ.get("TRADER_CRYPTO_PAIR", "XBTUSD"),
            "equity_ticker": os.environ.get("TRADER_EQUITY_TICKER", "TSLA"),
            "confidence_threshold": float(
                os.environ.get("TRADER_CONFIDENCE_THRESHOLD", "0.55")
            ),
        },
        "loop": {
            "last_receipt_ts": (last_receipt or {}).get("ts"),
            "last_receipt_lane": (last_receipt or {}).get("lane"),
            "last_receipt_symbol": (last_receipt or {}).get("symbol"),
            "receipts_last_5_min": recent_count,
            "alive_inference": recent_count > 0,
        },
        "trades": {
            "fires_today": fires_today,
            "spent_today_usd": spent_today,
            "last_execution_ts": (last_execution or {}).get("ts"),
            "last_execution_lane": (last_execution or {}).get("lane"),
            "last_execution_action": (last_execution or {}).get("action"),
            "last_execution_broker": (last_execution or {}).get("broker"),
            "last_execution_ok": (last_execution or {}).get("ok"),
        },
        "state": state.snapshot(),
        "checked_at": _now_iso(),
    }


@router.get("/health")
async def trader_health(_: dict = Depends(get_current_user)) -> dict:
    """Local store health: row counts + Mongo mirror lag. Reads
    only from SQLite; does not touch Mongo."""
    state, store = _import_trader()
    return {
        "ok": True,
        "store": store.counts(),
        "state": state.snapshot(),
        "checked_at": _now_iso(),
    }


@router.get("/receipts")
async def trader_receipts(
    _: dict = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=500),
    lane: Optional[str] = Query(default=None),
    fired_only: bool = Query(default=False),
) -> dict:
    """Most recent per-cycle receipts, from local SQLite. Answers:
        what did the trader see this minute?
        what did each brain say?
        what did the seat doctrine pick?
        did risk block? did broker accept?
    """
    _, store = _import_trader()
    rows = store.recent_receipts(limit=limit, lane=lane, fired_only=fired_only)
    return {"ok": True, "count": len(rows), "items": rows}


@router.get("/executions")
async def trader_executions(
    _: dict = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=500),
    lane: Optional[str] = Query(default=None),
    ok: Optional[bool] = Query(default=None),
) -> dict:
    """Executions written by the trader, from local SQLite. Each row
    carries the broker_response or exception_msg — 'what did
    Kraken/Webull actually say?' tape."""
    _, store = _import_trader()
    rows = store.recent_executions(limit=limit, lane=lane, ok=ok)
    return {"ok": True, "count": len(rows), "items": rows}


@router.post("/reload-caches")
async def trader_reload_caches(actor: dict = Depends(get_current_user)) -> dict:
    """Force an out-of-band pull from Mongo → in-memory cache. Used
    after the operator changes a seat assignment or flips the master
    switch and doesn't want to wait for the 60s refresh interval."""
    state, _ = _import_trader()
    poked = state.request_manual_refresh()
    return {
        "ok": True,
        "manual_refresh_queued": poked,
        "note": (
            "Refresh worker will run within a second; results visible "
            "at GET /api/admin/trader/status → state.last_refresh_ok_ts."
        ) if poked else (
            "Refresh worker is not running (trader loop not started). "
            "Set TRADER_ENABLED=true to activate the background refresher."
        ),
        "reloaded_at": _now_iso(),
        "requested_by": actor.get("email"),
    }


@router.post("/prune")
async def trader_prune(
    actor: dict = Depends(get_current_user),
    days: int = Query(default=7, ge=1, le=365,
                      description="Retention window in days"),
    keep_pending: bool = Query(default=True,
                               description="Refuse to prune rows not yet mirrored to Mongo"),
) -> dict:
    """Retention trim. Keeps the last `days` days of local truth in
    SQLite; anything older is dropped and space is reclaimed via
    VACUUM. Mongo mirror (best-effort archive) is the long-tail
    store.

    By default (`keep_pending=true`) rows that have not yet been
    mirrored to Mongo are preserved — so an Atlas outage does NOT
    cause silent data loss when the operator hits prune.

    Safe to schedule nightly via cron / a scheduled fetch."""
    _, store = _import_trader()
    result = await asyncio.to_thread(store.prune, days, keep_pending=keep_pending)
    return {
        "ok": True,
        "days": days,
        "pruned_at": _now_iso(),
        "pruned_by": actor.get("email"),
        **result,
    }


# Operator-canonical angel→brain pairings for the trader.
# Documented in /app/trader/state.py::DEFAULT_SEATS. Repeated here so
# the seed endpoint can write them without importing the trader
# module (decoupled from trader's lifecycle).
_OPERATOR_SEAT_PAIRINGS = [
    # Equity lane
    {"lane": "equity", "role": "strategist", "angel": "Raziel",  "holder": "camino"},
    {"lane": "equity", "role": "governor",   "angel": "Nuriel",  "holder": "hellcat",
     "risk_multiplier": 1.0},
    {"lane": "equity", "role": "executor",   "angel": "Paschar", "holder": "gto"},
    {"lane": "equity", "role": "auditor",    "angel": "Sariel",  "holder": "barracuda"},
    # Crypto lane
    {"lane": "crypto", "role": "strategist", "angel": "Remiel",  "holder": "hellcat"},
    {"lane": "crypto", "role": "governor",   "angel": "Cassiel", "holder": "camino",
     "risk_multiplier": 1.0},
    {"lane": "crypto", "role": "executor",   "angel": "Israfel", "holder": "gto"},
    {"lane": "crypto", "role": "auditor",    "angel": "Zadkiel", "holder": "barracuda"},
]


@router.post("/seed-seats")
async def trader_seed_seats(actor: dict = Depends(get_current_user)) -> dict:
    """Idempotent seat-registry seeder.

    Writes the operator-canonical angel→brain pairings into the
    `seat_registry` collection (Mongo). Safe to call repeatedly —
    uses upsert semantics with `$set` so an existing row's other
    fields (like `last_changed_at` audit) are preserved.
    """
    now = _now_iso()
    results = []
    for p in _OPERATOR_SEAT_PAIRINGS:
        sid = f"{p['lane']}:{p['role']}"
        set_fields = {
            "lane": p["lane"],
            "role": p["role"],
            "angel": p["angel"],
            "holder": p["holder"],
            "assigned_by": (actor.get("email") or "operator-seed"),
            "reason": "seeded_by_admin_trader_seed_seats",
            "last_changed_at": now,
            "since": now,
        }
        if "risk_multiplier" in p:
            set_fields["risk_multiplier"] = p["risk_multiplier"]
        await db["seat_registry"].update_one(
            {"_id": sid},
            {"$set": set_fields, "$setOnInsert": {"_id": sid}},
            upsert=True,
        )
        results.append({
            "id": sid,
            "angel": p["angel"],
            "role": p["role"],
            "lane": p["lane"],
            "holder": p["holder"],
        })
    # After seeding Mongo, poke the trader's state cache so the
    # change is picked up without waiting for the 60s refresh.
    try:
        state, _ = _import_trader()
        state.request_manual_refresh()
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True,
        "applied_at": now,
        "applied_by": actor.get("email"),
        "count": len(results),
        "seats": results,
    }
