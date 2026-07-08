"""Capital ledger stale-reservation sweeper.

Doctrine (2026-02-20):
    A crashed executor mid-submit leaves the ledger holding a
    reservation that no live order is going to reconcile. Without a
    sweep, the reserved amount stays "held" forever, silently
    starving future intents of headroom.

    This worker calls `sweep_stale_reservations` on both lanes on a
    fixed cadence. `max_age_minutes` is intentionally conservative
    (default 30 min) — the normal executor path completes in seconds,
    so any open reservation older than 30 minutes is almost
    certainly a crash artifact.

    Lane-specific `max_age_minutes` accepted via env
    (`CAPITAL_LEDGER_STALE_EQUITY_MIN`,
    `CAPITAL_LEDGER_STALE_CRYPTO_MIN`). Crypto trades 24/7 with
    highly variable fill latency, so default is more forgiving.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from shared.capital.ledger import sweep_stale_reservations

logger = logging.getLogger("capital_ledger_sweeper")


DEFAULT_TICK_INTERVAL_SEC = 300   # 5 min
DEFAULT_EQUITY_STALE_MIN = 30
DEFAULT_CRYPTO_STALE_MIN = 60


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key) or default)
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key)
    if val is None or val == "":
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


_stop_flag: bool = False
_task: Optional[asyncio.Task] = None


async def _tick() -> dict:
    """One sweep pass across both lanes."""
    equity_min = _env_int(
        "CAPITAL_LEDGER_STALE_EQUITY_MIN", DEFAULT_EQUITY_STALE_MIN,
    )
    crypto_min = _env_int(
        "CAPITAL_LEDGER_STALE_CRYPTO_MIN", DEFAULT_CRYPTO_STALE_MIN,
    )
    eq = await sweep_stale_reservations("equity", max_age_minutes=equity_min)
    cr = await sweep_stale_reservations("crypto", max_age_minutes=crypto_min)
    return {"equity": eq, "crypto": cr}


async def _worker_loop() -> None:
    global _stop_flag
    interval = _env_int(
        "CAPITAL_LEDGER_SWEEP_INTERVAL_SEC", DEFAULT_TICK_INTERVAL_SEC,
    )
    logger.info(
        "capital_ledger_sweeper started: interval=%ss "
        "equity_stale_min=%s crypto_stale_min=%s",
        interval,
        _env_int("CAPITAL_LEDGER_STALE_EQUITY_MIN", DEFAULT_EQUITY_STALE_MIN),
        _env_int("CAPITAL_LEDGER_STALE_CRYPTO_MIN", DEFAULT_CRYPTO_STALE_MIN),
    )
    while not _stop_flag:
        try:
            result = await _tick()
            total_released = (
                (result.get("equity") or {}).get("released", 0)
                + (result.get("crypto") or {}).get("released", 0)
            )
            if total_released > 0:
                logger.info(
                    "capital_ledger_sweeper tick: equity=%s crypto=%s",
                    result.get("equity"), result.get("crypto"),
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("capital_ledger_sweeper tick error: %r", e)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break


def start_worker_if_enabled() -> None:
    """Spawn the sweeper. Idempotent — safe on hot reload."""
    global _task, _stop_flag
    if _task is not None and not _task.done():
        return
    if not _env_bool("CAPITAL_LEDGER_SWEEPER_ENABLED", True):
        logger.info(
            "capital_ledger_sweeper disabled via "
            "CAPITAL_LEDGER_SWEEPER_ENABLED=false",
        )
        return
    _stop_flag = False
    _task = asyncio.create_task(
        _worker_loop(), name="capital_ledger_sweeper",
    )


async def stop_worker() -> None:
    global _task, _stop_flag
    _stop_flag = True
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _task = None


async def run_now() -> dict:
    """Manual one-shot invocation — used by admin re-trigger."""
    return await _tick()
