"""GTO runner — thin shim. Doctrine in `strategy.py`."""
from __future__ import annotations

from typing import Any

from shared.brains._runner_core import run_tick_for_brain
from shared.brains.gto import strategy as gto_strategy


TICK_LOG_COLLECTION = "gto_native_runtime_ticks"


async def tick_once(db) -> dict[str, Any]:
    return await run_tick_for_brain(
        db=db,
        brain_id="gto",
        strategy_fn=gto_strategy.evaluate,
        tick_log_collection=TICK_LOG_COLLECTION,
        runtime_version="v1",
    )


__all__ = ["tick_once", "TICK_LOG_COLLECTION"]
