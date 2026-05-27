"""Runtime-token rejection audit (2026-05-26).

When a brain POSTs with an invalid `X-Runtime-Token`, the request 401s
and the payload is dropped — by design. But that means a brain hammering
MC with a misaligned token can rack up tens of thousands of silent
rejections that the operator never sees. (REDEYE was bouncing ~21k
intents that way.)

This module persists a slim audit row per rejection so the operator
can see, per brain, how many auth failures occurred and over what
window. The audit is best-effort and ALWAYS fail-safe — if the audit
write fails for any reason the original 401 still surfaces unmolested.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger("risedual.runtime_token_audit")

COLLECTION = "runtime_token_rejections"


async def _async_record(runtime: str, reason: str) -> None:
    try:
        from db import db
        await db[COLLECTION].insert_one({
            "runtime": runtime,
            "reason": reason,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:  # noqa: BLE001
        logger.debug("runtime_token_audit write failed: %s", exc)


def record_rejection(runtime: str, reason: str) -> None:
    """Fire-and-forget — called from sync `verify_runtime_token`."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (rare) — drop silently. Worst case: we lose
        # one audit row; the 401 still fires.
        return
    loop.create_task(_async_record(runtime, reason))
