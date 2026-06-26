"""Hellcat native runtime scheduler — single asyncio task in MC.

Doctrinal interpretation lives at `shared/brains/hellcat/strategy.py`.

Flag-gating:
    HELLCAT_NATIVE_RUNTIME_ENABLED   default false
    HELLCAT_NATIVE_RUNTIME_TICK_SEC  default 60
"""
from __future__ import annotations

from shared.runtime._brain_scheduler import BrainScheduler


_scheduler: BrainScheduler | None = None


def _instance() -> BrainScheduler:
    global _scheduler
    if _scheduler is None:
        async def _tick() -> dict:
            from db import db
            from shared.brains.hellcat.runner import tick_once
            return await tick_once(db)

        _scheduler = BrainScheduler(
            brain_id="hellcat",
            enabled_env="HELLCAT_NATIVE_RUNTIME_ENABLED",
            tick_sec_env="HELLCAT_NATIVE_RUNTIME_TICK_SEC",
            tick_fn=_tick,
        )
    return _scheduler


def is_enabled() -> bool:
    return _instance().is_enabled()


def start_worker() -> None:
    _instance().start()


async def stop_worker() -> None:
    await _instance().stop()


__all__ = ["start_worker", "stop_worker", "is_enabled"]
