"""Async worker that fires daily snapshot captures on a schedule.

The loop wakes once per minute, checks the US/Eastern wall clock,
and triggers a capture when:
  - Today is a NYSE trading day (`is_trading_day()`).
  - The current time matches one of `SNAPSHOT_TIMES_ET` within a
    60-second window.
  - That (market_day, label) capture hasn't already run today
    (idempotency check via `daily_snapshot_capture_log`).

At the `open` capture each trading day, the worker also runs
`wipe_old_snapshots()` so retention is enforced lazily — no separate
midnight job.

Doctrine:
  - Read-only on brokers. The capture path only touches
    `shared_ohlcv_bars` and the Finnhub news cache.
  - One asyncio task only; idempotent on hot reload.
  - Disable via `MC_SNAPSHOT_WORKER_ENABLED=false`.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from db import db as _default_db
from namespaces import DAILY_SNAPSHOT_CAPTURE_LOG
from shared.snapshots.nyse_calendar import (
    NYSE_TZ,
    is_trading_day,
    market_day_today,
    now_eastern,
)
from shared.snapshots.service import (
    SNAPSHOT_LABELS,
    SNAPSHOT_TIMES_ET,
    capture_snapshot,
    wipe_old_snapshots,
)


logger = logging.getLogger("risedual.daily_snapshot_worker")


_TASK: Optional[asyncio.Task] = None
_STOP_FLAG: bool = False

# How close to the scheduled minute we accept the capture trigger.
# 60s means we tolerate a one-minute drift in the sleep loop.
TRIGGER_WINDOW_SEC: int = 60

# Sleep cadence between trigger-checks. 30s gives the worker tight
# tolerance vs. the 60s trigger window.
TICK_INTERVAL_SEC: int = 30


def _is_enabled() -> bool:
    return os.environ.get("MC_SNAPSHOT_WORKER_ENABLED", "true").lower() == "true"


async def _already_captured(market_day_str: str, label: str, db) -> bool:
    """Idempotency check — did the (market_day, label) capture run already?"""
    row = await db[DAILY_SNAPSHOT_CAPTURE_LOG].find_one(
        {"market_day": market_day_str, "label": label},
        {"_id": 1},
    )
    return row is not None


def _due_label(now_et: datetime) -> Optional[str]:
    """Returns the label whose scheduled time is within the current
    trigger window, or None."""
    minutes_now = now_et.hour * 60 + now_et.minute
    for label, (h, m) in SNAPSHOT_TIMES_ET.items():
        target = h * 60 + m
        # tolerate +/- (TRIGGER_WINDOW_SEC/60) minutes
        if abs(minutes_now - target) <= (TRIGGER_WINDOW_SEC // 60):
            return label
    return None


async def _tick(db) -> None:
    """One pass of the worker loop. Public for testability."""
    now_et = now_eastern()
    if not is_trading_day(now_et.date()):
        return
    label = _due_label(now_et)
    if label is None:
        return
    market_day_str = market_day_today().isoformat()
    if await _already_captured(market_day_str, label, db):
        return
    # On the `open` capture each trading day, enforce retention first
    # so yesterday's `close` doesn't briefly co-exist with today's
    # `open` of an unrelated week. Order matters: wipe THEN capture so
    # the new row isn't itself nuked by an off-by-one.
    if label == "open":
        try:
            await wipe_old_snapshots(db=db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("snapshot wipe failed: %s", exc)
    try:
        summary = await capture_snapshot(label, db=db)
        logger.info("snapshot worker fired: %s", summary)
    except Exception as exc:  # noqa: BLE001
        logger.exception("snapshot capture failed: %s", exc)


async def _loop() -> None:
    """Main worker loop. Sleeps `TICK_INTERVAL_SEC` between checks."""
    global _STOP_FLAG
    logger.info(
        "daily_snapshot worker started: labels=%s times_ET=%s",
        SNAPSHOT_LABELS,
        SNAPSHOT_TIMES_ET,
    )
    while not _STOP_FLAG:
        try:
            await _tick(_default_db)
        except Exception as exc:  # noqa: BLE001
            logger.exception("snapshot worker tick crashed: %s", exc)
        try:
            await asyncio.sleep(TICK_INTERVAL_SEC)
        except asyncio.CancelledError:
            break


def start_worker_if_enabled() -> None:
    """Spawn the task. Idempotent across hot reloads."""
    global _TASK, _STOP_FLAG
    if not _is_enabled():
        logger.info("daily_snapshot worker disabled (MC_SNAPSHOT_WORKER_ENABLED!=true)")
        return
    if _TASK is not None and not _TASK.done():
        return
    _STOP_FLAG = False
    _TASK = asyncio.create_task(_loop(), name="daily_snapshot_worker")


async def stop_worker() -> None:
    global _TASK, _STOP_FLAG
    _STOP_FLAG = True
    if _TASK is not None and not _TASK.done():
        _TASK.cancel()
        try:
            await _TASK
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _TASK = None


__all__ = (
    "start_worker_if_enabled",
    "stop_worker",
    "_tick",  # exported for tests
    "_due_label",  # exported for tests
)
