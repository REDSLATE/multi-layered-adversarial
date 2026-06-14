"""Background sweeper that auto-resolves Phase 2 vote sessions whose
3-min window has expired.

Doctrine fit: a session expiring without quorum (or with no clear
majority) resolves to REJECT per the operator's spec. The sweeper is
pure timeout enforcement — it never opens sessions or casts votes.
"""
from __future__ import annotations

import asyncio
import logging

from shared.paradox_v2.vote_session import sweep_expired


logger = logging.getLogger("risedual.paradox_v2.vote_session_sweeper")

SWEEP_INTERVAL_SEC: int = 30

_STOP = asyncio.Event()


async def _driver(interval_sec: int) -> None:
    while not _STOP.is_set():
        try:
            r = await sweep_expired()
            if r.get("swept"):
                logger.info("vote_session_sweep swept=%s", r["swept"])
        except Exception as e:  # noqa: BLE001
            logger.warning("vote_session_sweep failed: %s", e)
        try:
            await asyncio.wait_for(_STOP.wait(), timeout=interval_sec)
        except asyncio.TimeoutError:
            pass


async def start_vote_session_sweeper(interval_sec: int = SWEEP_INTERVAL_SEC) -> None:
    _STOP.clear()
    asyncio.create_task(_driver(interval_sec))
    logger.info("paradox_v2 vote_session_sweeper started interval=%ss", interval_sec)


async def stop_vote_session_sweeper() -> None:
    _STOP.set()
