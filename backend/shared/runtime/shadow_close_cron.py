"""Shadow-close cron — runs `shared.doctrine.shadow_outcome.run_shadow_close`
automatically at 4:05pm ET every trading day.

Operator directive (2026-02-19, late):
    "P1 — 4:05pm ET cron so shadow-close runs automatically at
     session end without manual click."

Doctrine:
  * Once-per-day idempotency — we record the date we last fired
    inside a module-level cache so the 60s tick loop doesn't
    re-run the engine within the same ET day. The shadow-close
    engine itself is also idempotent (the `$exists: false` guard
    in `join_outcome_to_doctrine`) so even if this trips twice
    on the same day it can't corrupt the counter.
  * Weekend skip — Sat/Sun in ET don't run. A holiday calendar is
    out of scope for tonight; the operator can toggle
    `SHADOW_CLOSE_CRON_ENABLED=false` on known holidays.
  * Fail-soft — any error in a tick is logged and swallowed. We
    never let the cron crash the pod or stall the event loop.
  * Operator override — `SHADOW_CLOSE_CRON_ENABLED` (default true)
    disables the worker entirely; admin endpoints
    (`/api/admin/outcome-join/shadow-close`) still work manually.

Trigger window:
  Fires when ET time is between 16:00:00 and 16:14:59 (close + 15
  min) AND we haven't fired yet today. 4:05pm is the target —
  catching the entire window means a one-tick blip can't make us
  skip the day.

Configuration env vars:
  SHADOW_CLOSE_CRON_ENABLED       (bool, default true)
  SHADOW_CLOSE_CRON_TICK_SEC      (int,  default 60)
  SHADOW_CLOSE_CRON_HOUR_ET       (int,  default 16)   # 4pm
  SHADOW_CLOSE_CRON_MIN_ET        (int,  default 5)    # :05
  SHADOW_CLOSE_CRON_WINDOW_MIN    (int,  default 14)   # 16:00-16:14 catches the fire
  SHADOW_CLOSE_CRON_MAX_ROWS      (int,  default 2000)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("risedual.shadow_close_cron")

_ET = ZoneInfo("America/New_York")

# Worker state — module-level so a hot-reload during dev doesn't lose
# the "fired today" mark (the underlying `outcome_join` guard would
# still no-op a re-fire, but logging double-runs is noisy).
_worker_task: Optional[asyncio.Task] = None
_last_fired_date: Optional[str] = None  # ET YYYY-MM-DD


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _now_et() -> datetime:
    return datetime.now(timezone.utc).astimezone(_ET)


def _should_fire(now_et: datetime) -> bool:
    """True iff (a) it's a weekday, (b) we're in the daily firing
    window, and (c) we haven't already fired today."""
    global _last_fired_date
    if now_et.weekday() >= 5:  # 5=Sat, 6=Sun
        return False

    target_hour = _env_int("SHADOW_CLOSE_CRON_HOUR_ET", 16)
    window_min = _env_int("SHADOW_CLOSE_CRON_WINDOW_MIN", 14)

    # Daily window: [target_hour:00, target_hour:00+window_min].
    # The target minute is informational ("we aim for 4:05pm") — the
    # window itself starts at the top of the hour so a slow tick can
    # still fire by 4:14pm.
    if now_et.hour != target_hour:
        return False
    # window in minutes from the top of the hour
    if now_et.minute > window_min:
        return False

    today_et = now_et.strftime("%Y-%m-%d")
    if _last_fired_date == today_et:
        return False
    # Touch the marker BEFORE firing so a slow `run_shadow_close`
    # can't cause a re-entry on the next 60s tick.
    _last_fired_date = today_et
    return True


async def _tick() -> None:
    """Single tick — check the time window and fire if eligible."""
    if not _should_fire(_now_et()):
        return
    try:
        # Lazy import — the cron module imports cleanly even if the
        # shadow-close engine has a syntax error during dev (we'd
        # rather surface that as a tick log warning than a
        # supervisor-crashing import error at startup).
        from shared.doctrine.shadow_outcome import run_shadow_close
        max_rows = _env_int("SHADOW_CLOSE_CRON_MAX_ROWS", 2000)
        result = await run_shadow_close(dry_run=False, max_rows=max_rows)
        logger.info(
            "shadow_close_cron fired: joined=%d considered=%d skipped=%s "
            "stockfit_remaining=%s",
            result.get("joined"), result.get("considered"),
            result.get("skipped"), result.get("stockfit_daily_remaining"),
        )
    except Exception as e:  # noqa: BLE001
        # Log + swallow — a cron failure must never crash the event loop
        # or take down the pod. Operator can re-run via the manual
        # endpoint if the cron fails.
        logger.warning("shadow_close_cron tick error: %r", e)


async def _loop() -> None:
    tick_sec = _env_int("SHADOW_CLOSE_CRON_TICK_SEC", 60)
    logger.info(
        "shadow_close_cron started: tick=%ss target=16:%02d ET",
        tick_sec, _env_int("SHADOW_CLOSE_CRON_MIN_ET", 5),
    )
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("shadow_close_cron loop error: %r", e)
        await asyncio.sleep(tick_sec)


def start_worker() -> None:
    """Start the cron worker. No-op if already running or disabled."""
    global _worker_task
    if not _env_bool("SHADOW_CLOSE_CRON_ENABLED", True):
        logger.info(
            "shadow_close_cron disabled via SHADOW_CLOSE_CRON_ENABLED=false",
        )
        return
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_loop())


async def stop_worker() -> None:
    """Cancel the cron worker (graceful shutdown)."""
    global _worker_task
    task = _worker_task
    _worker_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def status() -> dict:
    """Operator-facing snapshot for the admin endpoint."""
    now_et = _now_et()
    return {
        "enabled": _env_bool("SHADOW_CLOSE_CRON_ENABLED", True),
        "task_alive": _worker_task is not None and not _worker_task.done(),
        "now_et": now_et.isoformat(),
        "last_fired_date_et": _last_fired_date,
        "target_window_et": (
            f"{_env_int('SHADOW_CLOSE_CRON_HOUR_ET', 16):02d}:00 — "
            f"{_env_int('SHADOW_CLOSE_CRON_HOUR_ET', 16):02d}:"
            f"{_env_int('SHADOW_CLOSE_CRON_WINDOW_MIN', 14):02d}"
        ),
        "would_fire_now": _should_fire_dry_check(),
    }


def _should_fire_dry_check() -> bool:
    """Pure-read version of `_should_fire` for the status endpoint.
    Does NOT mutate the `_last_fired_date` marker."""
    now_et = _now_et()
    if now_et.weekday() >= 5:
        return False
    target_hour = _env_int("SHADOW_CLOSE_CRON_HOUR_ET", 16)
    window_min = _env_int("SHADOW_CLOSE_CRON_WINDOW_MIN", 14)
    if now_et.hour != target_hour or now_et.minute > window_min:
        return False
    return _last_fired_date != now_et.strftime("%Y-%m-%d")


def reset_for_tests() -> None:
    """Tests rebind the singleton. Production never calls this."""
    global _worker_task, _last_fired_date
    _worker_task = None
    _last_fired_date = None
