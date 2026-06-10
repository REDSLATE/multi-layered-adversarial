"""Public.com broker-fills ingestor — MC's source of broker truth.

Doctrine pin (operator directive, 2026-06-10, post-AAPL incident):

    The brains have to stop being amnesiac. Right now MC can only
    see its OWN intents — it has no idea what Public.com actually
    did with them. On 2026-06-09, MC posted 14 gate-passed BUY
    intents on AAPL and Public.com turned that into 130 actual
    fills. MC never knew. Every successive brain tick saw
    `current_side=FLAT` while the broker quietly built a 1.3279
    share long position.

    This module closes that gap. Every 20 seconds we walk Public's
    `/history` endpoint, normalize each transaction into a canonical
    fill row, and upsert into `shared_broker_fills`. The
    auto-router's dedupe layer (next pass) reads from this
    collection to answer "is there a pending order on AAPL right
    now?" before submitting a new one.

Doctrine:
    * Upserts are keyed by Public's transaction id — idempotent. We
      can re-poll any window without dirtying the collection.
    * We poll a 5-minute trailing window every 20 seconds. Public's
      eventual-consistency window is documented as "a few minutes"
      so 5 is the safety margin.
    * Failure modes (transport error, auth expired, no credentials)
      all fall through to a log line + retry next tick. Never
      raises into the lifespan.
    * Equity only this pass (Public.com). Kraken/crypto comes next
      via its own poller — Kraken's fill API is differently shaped.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db import db


logger = logging.getLogger("risedual.broker_fills")


# ── Collection + config ──────────────────────────────────────────

BROKER_FILLS_COLLECTION = "shared_broker_fills"

# Poll cadence — every 20s by default. Operator can override via env
# for slower / faster polling. We never go below 5s (Public.com rate
# limits would bite).
_POLL_INTERVAL_SEC = max(
    5, int(os.environ.get("BROKER_FILLS_POLL_INTERVAL_SEC", "20")),
)

# Trailing window we ask Public for each poll. 5 min is operator-
# pinned: long enough to catch any fill Public hasn't indexed yet,
# short enough to keep the response small.
_TRAILING_WINDOW_SEC = int(
    os.environ.get("BROKER_FILLS_TRAILING_WINDOW_SEC", "300"),
)

# Pending-order age-out — auto-router treats a fill row as "pending"
# only if it's younger than this. Anything older is assumed
# acknowledged / rejected / expired.
_PENDING_TTL_SEC = int(
    os.environ.get("BROKER_FILLS_PENDING_TTL_SEC", "30"),
)


# ── Internal singletons ──────────────────────────────────────────

_TASK: Optional[asyncio.Task] = None
_STOP: Optional[asyncio.Event] = None


# ── Normalization ────────────────────────────────────────────────


def _normalize_transaction(tx: dict, account_id: str) -> Optional[dict]:
    """Translate one Public.com `/history` transaction into MC's
    canonical fill shape.

    We keep ONLY actual trades — money movements, dividends, etc.
    are noise from MC's perspective and filtered out here.

    Returns None for non-TRADE rows.
    """
    if (tx.get("type") or "").upper() != "TRADE":
        return None
    sym = (tx.get("symbol") or "").upper()
    if not sym:
        return None
    side = (tx.get("side") or "").upper()
    try:
        qty = float(tx.get("quantity") or 0)
    except (TypeError, ValueError):
        qty = 0.0
    try:
        net = float(tx.get("netAmount") or 0)
    except (TypeError, ValueError):
        net = 0.0

    # Price is parsed out of the human-readable description when the
    # raw price field isn't broken out — Public's docs are weak on
    # this. Fallback: |net / qty|.
    price = None
    desc = tx.get("description") or ""
    if " at " in desc:
        try:
            price = float(desc.split(" at ")[-1].strip().rstrip("."))
        except (ValueError, IndexError):
            price = None
    if price is None and qty:
        price = abs(net / qty)

    return {
        "_id": tx.get("id"),                # idempotent upsert key
        "broker": "public",
        "account_id": account_id,
        "symbol": sym,
        "side": side,
        "qty": qty,
        "price": price,
        "net_amount": net,
        "fees": float(tx.get("fees") or 0),
        "timestamp": tx.get("timestamp"),
        "subType": tx.get("subType"),
        "security_type": tx.get("securityType"),
        "direction": tx.get("direction"),
        "raw": tx,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Poll cycle ───────────────────────────────────────────────────


async def _poll_once() -> int:
    """One pass through `/history`. Returns count of rows upserted."""
    from shared.broker_router import _get_equity_adapter  # type: ignore
    adapter = await _get_equity_adapter()
    if adapter is None:
        return 0

    now = datetime.now(timezone.utc)
    start = (now - timedelta(seconds=_TRAILING_WINDOW_SEC)).replace(microsecond=0)
    # Public's start/end format requires Z suffix and no microseconds.
    start_iso = start.isoformat().replace("+00:00", "Z")
    end_iso = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    try:
        rows = await adapter.list_history(
            start=start_iso, end=end_iso,
            page_size=200, max_pages=5,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "broker_fills poll failed: %s — retrying next tick", exc,
        )
        return 0

    if not rows:
        return 0

    n_up = 0
    for tx in rows:
        norm = _normalize_transaction(tx, adapter.account_id)
        if not norm or not norm.get("_id"):
            continue
        try:
            await db[BROKER_FILLS_COLLECTION].update_one(
                {"_id": norm["_id"]},
                {"$set": norm},
                upsert=True,
            )
            n_up += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "broker_fills upsert failed id=%s err=%s",
                norm.get("_id"), exc,
            )
    return n_up


async def _poll_loop() -> None:
    """Run forever — poll, sleep, poll. Cancelled via `_STOP` event."""
    assert _STOP is not None
    logger.info(
        "broker_fills poller starting interval=%ds window=%ds",
        _POLL_INTERVAL_SEC, _TRAILING_WINDOW_SEC,
    )
    while not _STOP.is_set():
        try:
            n = await _poll_once()
            if n:
                logger.info("broker_fills upserted %d rows", n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("broker_fills loop error: %s", exc)
        try:
            await asyncio.wait_for(_STOP.wait(), timeout=_POLL_INTERVAL_SEC)
        except asyncio.TimeoutError:
            continue
    logger.info("broker_fills poller stopped")


def start_broker_fills_poller() -> None:
    """Lifespan hook — idempotent. Starts the poller if not already
    running and Public credentials exist."""
    global _TASK, _STOP  # noqa: PLW0603
    if _TASK is not None and not _TASK.done():
        return
    _STOP = asyncio.Event()
    _TASK = asyncio.create_task(_poll_loop(), name="broker_fills_poller")


async def stop_broker_fills_poller() -> None:
    """Lifespan shutdown hook."""
    global _TASK, _STOP  # noqa: PLW0603
    if _STOP is not None:
        _STOP.set()
    if _TASK is not None:
        try:
            await asyncio.wait_for(_TASK, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    _TASK = None
    _STOP = None


# ── Accessors (read-only, for gate chain + dashboards) ────────────


async def get_recent_fills(
    symbol: Optional[str] = None,
    minutes: int = 5,
    limit: int = 200,
) -> list[dict]:
    """Return recent normalized fills, newest first.

    Filter by symbol if provided. `minutes` is the trailing window.
    """
    since = (
        datetime.now(timezone.utc) - timedelta(minutes=minutes)
    ).isoformat().replace("+00:00", "Z")
    q: dict[str, Any] = {"timestamp": {"$gte": since}}
    if symbol:
        q["symbol"] = symbol.upper()
    cur = (
        db[BROKER_FILLS_COLLECTION]
        .find(q, {"raw": 0})
        .sort("timestamp", -1)
        .limit(limit)
    )
    out: list[dict] = []
    async for d in cur:
        out.append(d)
    return out


async def get_pending_orders_for_symbol(symbol: str) -> list[dict]:
    """Return fills for `symbol` that are still inside the pending
    TTL — these are the orders the auto-router must treat as
    "in flight" and refuse to duplicate.

    Doctrine pin: the AAPL 06-09 incident happened because MC could
    not answer this question. Now it can.
    """
    since = (
        datetime.now(timezone.utc) - timedelta(seconds=_PENDING_TTL_SEC)
    ).isoformat().replace("+00:00", "Z")
    cur = db[BROKER_FILLS_COLLECTION].find(
        {"symbol": symbol.upper(), "timestamp": {"$gte": since}},
        {"raw": 0},
    ).sort("timestamp", -1)
    out: list[dict] = []
    async for d in cur:
        out.append(d)
    return out


async def has_pending_order(symbol: str) -> bool:
    """Fast yes/no for the auto-router's dedupe check.

    True if ANY broker fill for this symbol landed within the
    pending TTL window — that's our signal that an order is still
    in flight or just acknowledged. Auto-router must not submit
    another until this returns False.
    """
    since = (
        datetime.now(timezone.utc) - timedelta(seconds=_PENDING_TTL_SEC)
    ).isoformat().replace("+00:00", "Z")
    doc = await db[BROKER_FILLS_COLLECTION].find_one(
        {"symbol": symbol.upper(), "timestamp": {"$gte": since}},
        {"_id": 1},
    )
    return doc is not None


__all__ = [
    "BROKER_FILLS_COLLECTION",
    "start_broker_fills_poller", "stop_broker_fills_poller",
    "get_recent_fills", "get_pending_orders_for_symbol",
    "has_pending_order",
]
