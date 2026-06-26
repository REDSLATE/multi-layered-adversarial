"""Shared scheduler core for native brain runtimes.

One generic env-gated background-task driver. Each brain's
`shared/runtime/<brain>_runtime.py` is a thin shim that supplies its
own env-flag prefix and tick callable.

Doctrine:
    flag-gated by `<BRAIN>_NATIVE_RUNTIME_ENABLED` (default False)
    tick interval `<BRAIN>_NATIVE_RUNTIME_TICK_SEC` (default 60)
    whole-tick failures never kill the loop
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Awaitable, Callable, Optional


logger = logging.getLogger("risedual.brains.scheduler_core")


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "scheduler_core: bad %s=%r — falling back to %s",
            name, raw, default,
        )
        return default


TickFn = Callable[[], Awaitable[dict]]


class BrainScheduler:
    """Per-brain singleton-style scheduler. One instance per native
    runtime module — `barracuda_runtime.py` constructs one, etc.

    The class is deliberately thin: it owns just enough state to start
    / stop a single `asyncio.Task` and report whether it's enabled.
    All "what to do per tick" lives in the supplied `tick_fn`.
    """

    def __init__(
        self,
        *,
        brain_id: str,
        enabled_env: str,
        tick_sec_env: str,
        enabled_default: bool = False,
        tick_sec_default: int = 60,
        tick_fn: TickFn,
    ):
        self.brain_id = brain_id
        self._enabled_env = enabled_env
        self._tick_sec_env = tick_sec_env
        self._enabled_default = enabled_default
        self._tick_sec_default = tick_sec_default
        self._tick_fn = tick_fn
        self._task: Optional[asyncio.Task] = None
        self._logger = logging.getLogger(
            f"risedual.brains.{brain_id}.runtime",
        )

    def is_enabled(self) -> bool:
        return env_bool(self._enabled_env, self._enabled_default)

    async def _loop(self) -> None:
        tick_sec = env_int(self._tick_sec_env, self._tick_sec_default)
        self._logger.info(
            "%s native runtime started: tick=%ss (enabled via %s)",
            self.brain_id, tick_sec, self._enabled_env,
        )
        while True:
            try:
                summary = await self._tick_fn()
                self._logger.info(
                    "%s tick: universe=%d emitted=%d skipped=%d "
                    "no_snapshot=%d errors=%d",
                    self.brain_id,
                    summary.get("universe_size", 0),
                    summary.get("emitted_count", 0),
                    summary.get("skipped_count", 0),
                    summary.get("no_snapshot_count", 0),
                    summary.get("error_count", 0),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._logger.exception(
                    "%s tick failed: %r", self.brain_id, exc,
                )
            await asyncio.sleep(tick_sec)

    def start(self) -> None:
        if not self.is_enabled():
            self._logger.info(
                "%s native runtime DISABLED via %s — staying dormant",
                self.brain_id, self._enabled_env,
            )
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._loop(), name=f"{self.brain_id}_native_runtime",
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    @property
    def task(self) -> Optional[asyncio.Task]:
        return self._task


__all__ = ["BrainScheduler", "env_bool", "env_int"]
