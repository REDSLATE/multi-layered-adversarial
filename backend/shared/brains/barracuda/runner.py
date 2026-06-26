"""Barracuda runner — thin shim binding the brain's strategy to the
shared `_runner_core.run_tick_for_brain` plumbing.

Doctrine isolation: the mean-reversion interpretation lives in
`strategy.py`. This module just wires it to the canonical emit path.
"""
from __future__ import annotations

from typing import Any

from shared.brains._runner_core import run_tick_for_brain
from shared.brains.barracuda import strategy as barracuda_strategy


TICK_LOG_COLLECTION = "barracuda_native_runtime_ticks"


async def tick_once(db) -> dict[str, Any]:
    """One full pass over the equity universe. Returns the summary
    dict the scheduler logs and the tick log persists."""
    return await run_tick_for_brain(
        db=db,
        brain_id="barracuda",
        strategy_fn=barracuda_strategy.evaluate,
        tick_log_collection=TICK_LOG_COLLECTION,
        runtime_version="v1",
    )


__all__ = ["tick_once", "TICK_LOG_COLLECTION"]
