"""FastAPI lifespan helpers for the PARADOX coordinator."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from shared.coordinator.runner import coordinator_loop, request_stop

log = logging.getLogger("risedual.paradox_coordinator")

_task: Optional[asyncio.Task] = None


async def start_paradox_coordinator() -> None:
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(coordinator_loop())
    log.info("paradox_coordinator: armed (all agents disabled by default)")


async def stop_paradox_coordinator() -> None:
    global _task
    request_stop()
    if _task is not None:
        try:
            await asyncio.wait_for(_task, timeout=5.0)
        except asyncio.TimeoutError:
            _task.cancel()
            try:
                await _task
            except (asyncio.CancelledError, Exception):
                pass
    _task = None
