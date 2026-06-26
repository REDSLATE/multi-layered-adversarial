"""Barracuda native runtime scheduler — single asyncio task in MC.

Replaces the external Barracuda sidecar. One coroutine loops every
`BARRACUDA_NATIVE_RUNTIME_TICK_SEC` seconds (default 60) and calls
`shared.brains.barracuda.runner.tick_once(db)`.

Doctrine (per operator directive, 2026-02-23):
    brains think separately
    MC schedules them together
    only canonical pipeline emits
    only seat holder can execute

This module is the "schedules them together" layer for Barracuda. It
does not implement the brain's interpretation function — that lives
in `shared/brains/barracuda/strategy.py`.

Lifecycle:
    `start_worker()` — called from `server_modules/lifespan.py` startup
    `stop_worker()` — called from the lifespan shutdown path

Flag-gating:
    Env `BARRACUDA_NATIVE_RUNTIME_ENABLED` — default `false`. Operator
    flips to `true` to flip MC into "Barracuda runs in-process" mode.
    When false the worker is a no-op; the external sidecar (if any
    is still alive) keeps running.

    Env `BARRACUDA_NATIVE_RUNTIME_TICK_SEC` — default 60. Per-tick
    interval. Reads once at start; restart the process to change.

Observability:
    Each tick writes a row to `barracuda_native_runtime_ticks`
    (handled by `runner.tick_once`). The supervisor log gets one
    INFO line per tick with the headline counts.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional


logger = logging.getLogger("risedual.brains.barracuda.runtime")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "barracuda runtime: bad %s=%r — falling back to %s",
            name, raw, default,
        )
        return default


ENABLED_DEFAULT = False
TICK_SEC_DEFAULT = 60


_worker_task: Optional[asyncio.Task] = None


def is_enabled() -> bool:
    return _env_bool("BARRACUDA_NATIVE_RUNTIME_ENABLED", ENABLED_DEFAULT)


async def _loop() -> None:
    from db import db
    from shared.brains.barracuda.runner import tick_once

    tick_sec = _env_int(
        "BARRACUDA_NATIVE_RUNTIME_TICK_SEC", TICK_SEC_DEFAULT,
    )
    logger.info(
        "barracuda native runtime started: tick=%ss (enabled via "
        "BARRACUDA_NATIVE_RUNTIME_ENABLED)", tick_sec,
    )

    while True:
        try:
            summary = await tick_once(db)
            logger.info(
                "barracuda tick: universe=%d emitted=%d skipped=%d "
                "no_snapshot=%d errors=%d",
                summary.get("universe_size", 0),
                summary.get("emitted_count", 0),
                summary.get("skipped_count", 0),
                summary.get("no_snapshot_count", 0),
                summary.get("error_count", 0),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # A whole-tick failure (e.g. Mongo unreachable) must NEVER
            # kill the loop. Log and keep ticking.
            logger.exception("barracuda tick failed: %r", exc)
        await asyncio.sleep(tick_sec)


def start_worker() -> None:
    """Start the background task. No-op if disabled or already running."""
    global _worker_task
    if not is_enabled():
        logger.info(
            "barracuda native runtime DISABLED via "
            "BARRACUDA_NATIVE_RUNTIME_ENABLED — staying dormant",
        )
        return
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(_loop(), name="barracuda_native_runtime")


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


__all__ = ["start_worker", "stop_worker", "is_enabled"]
