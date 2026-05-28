"""Opinion-silent watchdog — background worker.

Doctrine:
    Mirrors `routes/opinion_silence_watchdog.py` (the operator-facing
    HTTP scan) but runs autonomously on a tick. Both surfaces share
    `perform_scan(...)` so there is exactly ONE silence-detection
    code path. Operator-on-demand scans and autonomous scans cannot
    diverge.

    ADVISORY OBSERVABILITY ONLY. The worker:
      * NEVER reassigns a seat
      * NEVER vetoes an intent
      * NEVER touches execution authority

    It writes one row per silent (brain, seat) per cooldown window
    to `opinion_silence_alerts`. The operator UI polls
    `GET /api/admin/opinion-silence-watchdog/recent` to surface them.

Config (env, with safe defaults):
    OPINION_SILENCE_WATCHDOG_ENABLED   true|false       default: true
    OPINION_SILENCE_WATCHDOG_TICK_SEC  int seconds      default: 900   (15 min)
    OPINION_SILENCE_THRESHOLD_SEC      int seconds      default: 14400 (4h)
    OPINION_SILENCE_COOLDOWN_SEC       int seconds      default: 1800  (30 min)

The defaults match the operator's Seat Roster strip orange chip
(OPINION_FRESH_SEC * 4 = 4h) so the watchdog cannot fire an alert
about a seat the operator's UI still calls "fresh".
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional


logger = logging.getLogger("risedual.opinion_silence_worker")


def _env_int(name: str, default: int) -> int:
    """Tolerant int env read — bad values fall back to default."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "opinion_silence_worker: bad %s=%r, falling back to %s",
            name, raw, default,
        )
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


WATCHDOG_ENABLED_DEFAULT = True
WATCHDOG_TICK_SEC_DEFAULT = 15 * 60       # 15 min
THRESHOLD_SEC_DEFAULT = 4 * 60 * 60       # 4h
COOLDOWN_SEC_DEFAULT = 30 * 60            # 30 min


_worker_task: Optional[asyncio.Task] = None


async def _loop() -> None:
    """Main background loop. Idempotent if called more than once."""
    tick_sec = _env_int(
        "OPINION_SILENCE_WATCHDOG_TICK_SEC", WATCHDOG_TICK_SEC_DEFAULT,
    )
    threshold_sec = _env_int(
        "OPINION_SILENCE_THRESHOLD_SEC", THRESHOLD_SEC_DEFAULT,
    )
    cooldown_sec = _env_int(
        "OPINION_SILENCE_COOLDOWN_SEC", COOLDOWN_SEC_DEFAULT,
    )
    logger.info(
        "opinion_silence_worker started: tick=%ss threshold=%ss cooldown=%ss",
        tick_sec, threshold_sec, cooldown_sec,
    )
    # Lazy import — keeps the route module the source of truth for
    # the scan logic, and avoids any circular-import risk at boot.
    from routes.opinion_silence_watchdog import perform_scan

    while True:
        try:
            result = await perform_scan(
                threshold_sec=threshold_sec,
                cooldown_sec=cooldown_sec,
                dry_run=False,
            )
            if result.get("flagged_count", 0) > 0:
                logger.warning(
                    "opinion_silence_worker tick: %d silent seat(s) flagged: %s",
                    result["flagged_count"],
                    [
                        f"{r['brain']}@{r['seat']}({r['kind']})"
                        for r in result.get("flagged", [])
                    ],
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("opinion_silence_worker tick error: %r", e)
        await asyncio.sleep(tick_sec)


def start_worker() -> None:
    """Start the background task. No-op if already running or disabled."""
    global _worker_task
    if not _env_bool(
        "OPINION_SILENCE_WATCHDOG_ENABLED", WATCHDOG_ENABLED_DEFAULT,
    ):
        logger.info(
            "opinion_silence_worker disabled via "
            "OPINION_SILENCE_WATCHDOG_ENABLED=false",
        )
        return
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_loop())


async def stop_worker() -> None:
    """Cancel the background task (graceful shutdown)."""
    global _worker_task
    task = _worker_task
    _worker_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
