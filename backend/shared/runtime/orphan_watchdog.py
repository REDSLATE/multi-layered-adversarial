"""
Orphan Watchdog — continuous broker-side surveillance.
======================================================

Polls Alpaca every N seconds for new filled orders, cross-checks each
against MC's intent/receipt ledger, and quarantines anything MC did
NOT issue.

Doctrine:
    MC is supposed to be the sole source of broker activity. Any fill
    on the broker that was not preceded by an MC intent is an ORPHAN
    — a rogue actor holds a raw API key and is firing outside MC.
    Orphans are auto-classified as UV (Unverified) and dropped into
    `memory_kernel_quarantine` with `alert_level=CRITICAL` so the
    operator sees them immediately on the diagnostics panel.

How orphan detection works:
    For each Alpaca fill seen since the last sweep:
      1. Insert (upsert) into `broker_orders`.
      2. Look for a matching `shared_intents` row tagged with this
         broker_order_id (MC stamps the broker order id back onto
         the intent at submission time). If found → legitimate
         MC trade; skip.
      3. No matching intent → ORPHAN. Submit through the memory
         kernel as `memory_type=execution`, source_stack=
         `alpaca_orphan_watchdog`. The kernel classifies UV and
         writes the CRITICAL quarantine row.

Configuration (env-driven):
    ALPACA_ORPHAN_WATCHDOG_ENABLED       = "true"|"false" (default false)
    ALPACA_INGEST_KEY_ID                 = paper API key id
    ALPACA_INGEST_SECRET_KEY             = paper API secret
    ALPACA_ORPHAN_WATCHDOG_INTERVAL_S    = sweep interval (default 120)
    ALPACA_BASE_URL                      = paper or live

The watchdog is wired into the FastAPI lifespan so it starts/stops with
the app. If keys are missing or `ENABLED=false`, the watchdog logs once
and exits cleanly — never crashes the server.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx

from db import db


log = logging.getLogger("risedual.orphan_watchdog")


_TASK: Optional[asyncio.Task] = None
_LAST_CURSOR_TS: Optional[str] = None  # ISO string; advances each sweep


def _enabled() -> bool:
    return str(os.environ.get("ALPACA_ORPHAN_WATCHDOG_ENABLED", "false")).lower() == "true"


def _interval_s() -> int:
    try:
        return max(30, int(os.environ.get("ALPACA_ORPHAN_WATCHDOG_INTERVAL_S", "120")))
    except ValueError:
        return 120


def _alpaca_base() -> str:
    return os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")


def _alpaca_headers() -> Optional[Dict[str, str]]:
    key = os.environ.get("ALPACA_INGEST_KEY_ID", "")
    sec = os.environ.get("ALPACA_INGEST_SECRET_KEY", "")
    if not key or not sec:
        return None
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}


async def _is_mc_issued(broker_order_id: str) -> bool:
    """An order is MC-issued iff at least one shared_intents row
    references this broker_order_id. MC stamps the id at submit time
    (see shared/execution.py::execution_submit)."""
    hit = await db.shared_intents.find_one(
        {"broker_order_id": broker_order_id},
        {"_id": 1},
    )
    return hit is not None


async def _upsert_broker_order(order: Dict[str, Any]) -> None:
    doc = {
        "broker_order_id": order["id"],
        "symbol": order.get("symbol"),
        "status": "FILLED",
        "filled_qty": float(order.get("filled_qty") or 0),
        "filled_avg_price": float(order.get("filled_avg_price") or 0),
        "side": order.get("side"),
        "submitted_at": order.get("submitted_at"),
        "filled_at": order.get("filled_at"),
        "source": order.get("source", "access_key"),
        "venue": "alpaca",
        "ingest_origin": "orphan_watchdog",
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.broker_orders.update_one(
        {"broker_order_id": order["id"]},
        {"$set": doc},
        upsert=True,
    )


async def _quarantine_orphan(order: Dict[str, Any]) -> None:
    """Lazy-import the kernel so this module has zero hard dependency
    on the kernel's import order during boot."""
    from services.brain_memory_translator import translate_brain_memory
    from services.memory_kernel import MemoryKernelLedger

    _stack, mtype, payload = translate_brain_memory(
        source_stack="alpaca_orphan_watchdog",
        memory_type="execution",
        payload={
            "symbol": order.get("symbol"),
            "broker_order_id": order["id"],
            # Deliberately omit execution_receipt_id — that's how the
            # kernel recognises the orphan.
            "filled_qty": float(order.get("filled_qty") or 0),
            "filled_avg_price": float(order.get("filled_avg_price") or 0),
            "side": order.get("side"),
            "submitted_at": order.get("submitted_at"),
            "filled_at": order.get("filled_at"),
            "alpaca_source": order.get("source", "access_key"),
            "ingest_note": "Live orphan caught by watchdog — broker fired without MC receipt.",
        },
    )
    ledger = MemoryKernelLedger(db)
    await ledger.submit_memory(
        source_stack="alpaca_orphan_watchdog",
        memory_type=mtype,
        payload=payload,
    )


async def _sweep_once(now_iso: str) -> Dict[str, int]:
    """One sweep: fetch fills since cursor, classify each, return counts."""
    global _LAST_CURSOR_TS

    headers = _alpaca_headers()
    if headers is None:
        return {"skipped": 1, "reason_no_keys": 1}

    # First-run cursor: look back 10 minutes.
    cursor = _LAST_CURSOR_TS or (
        (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    )

    params = {
        "status": "closed",
        "after": cursor,
        "until": now_iso,
        "limit": "500",
        "direction": "asc",
    }
    counts = {"checked": 0, "mc_issued": 0, "orphan": 0, "errors": 0}

    try:
        async with httpx.AsyncClient(timeout=30) as cli:
            r = await cli.get(
                f"{_alpaca_base()}/v2/orders",
                headers=headers,
                params=params,
            )
            r.raise_for_status()
            orders = r.json()
    except httpx.HTTPError as e:
        log.warning("orphan_watchdog: alpaca fetch failed: %s", e)
        return {"errors": 1}

    for o in orders:
        if o.get("status") != "filled":
            continue
        counts["checked"] += 1
        await _upsert_broker_order(o)
        if await _is_mc_issued(o["id"]):
            counts["mc_issued"] += 1
            continue
        try:
            await _quarantine_orphan(o)
            counts["orphan"] += 1
            log.warning(
                "orphan_watchdog: ORPHAN %s %s qty=%s @ %s id=%s src=%s",
                o.get("side"), o.get("symbol"), o.get("filled_qty"),
                o.get("filled_avg_price"), o.get("id"),
                o.get("source"),
            )
        except Exception as e:  # noqa: BLE001
            log.exception("orphan_watchdog: quarantine failed for %s: %s", o.get("id"), e)
            counts["errors"] += 1

    _LAST_CURSOR_TS = now_iso
    return counts


async def _loop() -> None:
    interval = _interval_s()
    log.info("orphan_watchdog: loop start (interval=%ds)", interval)
    while True:
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            counts = await _sweep_once(now_iso)
            if counts.get("orphan", 0) > 0:
                log.warning("orphan_watchdog: sweep complete %s", counts)
            else:
                log.debug("orphan_watchdog: sweep complete %s", counts)
        except asyncio.CancelledError:  # graceful shutdown
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("orphan_watchdog: loop error: %s", e)
        await asyncio.sleep(interval)


async def start_watchdog_if_enabled() -> None:
    global _TASK
    if _TASK and not _TASK.done():
        return
    if not _enabled():
        log.info("orphan_watchdog: disabled (set ALPACA_ORPHAN_WATCHDOG_ENABLED=true to arm)")
        return
    if _alpaca_headers() is None:
        log.warning("orphan_watchdog: missing ALPACA_INGEST_KEY_ID / SECRET — not starting")
        return
    _TASK = asyncio.create_task(_loop())
    log.info("orphan_watchdog: armed")


async def stop_watchdog() -> None:
    global _TASK
    if _TASK and not _TASK.done():
        _TASK.cancel()
        try:
            await _TASK
        except asyncio.CancelledError:
            pass
    _TASK = None
    log.info("orphan_watchdog: stopped")
