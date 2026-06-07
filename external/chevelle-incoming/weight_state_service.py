"""Persistent WeightState for the dynamic confidence-weighting system.

Doctrine (operator note May 2026 — "yes to the dynamic weight idea"):
    Each of the five voices (strategist, auditor, commander, regime,
    memory) earns its weight via recent track record. We persist the
    smoothed state so:
      1. weights survive process restarts (don't snap back to 1.0×5),
      2. the nightly scheduled refresh has a previous state to smooth
         against (alpha=0.30 in confidence_weighting.smooth),
      3. the decision pipeline can read the current weights without
         recomputing winrates on every call.

    Storage: a single-document mongo collection `engine_weight_state`
    keyed by `runtime` (chevelle / alpha / redeye / camaro). Idempotent
    upserts.

    During the cold-start period (no resolved outcomes in the last 20
    decisions) `compute_engine_winrates` returns 0.50 across the board
    → `compute_dynamic_weights` keeps everything at the smoothed-toward
    1.0 default → the weighted-average formula degenerates to a plain
    mean. Safe at boot, kicks in naturally as data accumulates.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from services.confidence_weighting import (
    WeightState,
    compute_dynamic_weights,
    compute_engine_winrates,
)

logger = logging.getLogger(__name__)

COLLECTION = "engine_weight_state"
RUNTIME = os.environ.get("RUNTIME_NAME", "chevelle").lower()


def _to_state(doc: Optional[dict]) -> WeightState:
    if not doc:
        return WeightState()
    return WeightState(
        strategist_weight=float(doc.get("strategist_weight", 1.0)),
        auditor_weight=float(doc.get("auditor_weight", 1.0)),
        commander_weight=float(doc.get("commander_weight", 1.0)),
        regime_weight=float(doc.get("regime_weight", 1.0)),
        memory_weight=float(doc.get("memory_weight", 1.0)),
    )


async def load(db: Any) -> WeightState:
    """Load the persisted WeightState for the current runtime. Returns
    defaults (all 1.0) when nothing has been persisted yet."""
    if db is None:
        return WeightState()
    try:
        doc = await db[COLLECTION].find_one({"runtime": RUNTIME}, {"_id": 0})
    except Exception as exc:                                # noqa: BLE001
        logger.warning(f"weight_state load failed: {exc}")
        return WeightState()
    return _to_state(doc)


async def refresh(db: Any, lookback: int = 20) -> dict[str, Any]:
    """Compute fresh winrates, smooth into a new WeightState, persist.

    Returns a dict with the new state + the winrate inputs + the
    resolved-decision count, suitable for logging or for MC's diagnostics
    surface. Failure-tolerant: any exception falls back to the previous
    state (or defaults) and logs the issue."""
    previous = await load(db)
    metrics = await compute_engine_winrates(db, lookback=lookback)
    try:
        new_state = compute_dynamic_weights(
            strategist_winrate_20=metrics["strategist_winrate_20"],
            auditor_winrate_20=metrics["auditor_winrate_20"],
            commander_alignment_rate=metrics["commander_alignment_rate"],
            regime_accuracy=metrics["regime_accuracy"],
            memory_match_winrate=metrics["memory_match_winrate"],
            current=previous,
        )
    except Exception as exc:                                # noqa: BLE001
        logger.warning(f"weight_state compute failed: {exc}")
        return {
            "runtime": RUNTIME,
            "state": previous.as_dict(),
            "metrics": metrics,
            "refreshed_at": None,
            "error": str(exc),
        }

    persisted_doc = {
        "runtime": RUNTIME,
        **new_state.as_dict(),
        "metrics": metrics,
        "refreshed_at": datetime.now(timezone.utc),
    }
    try:
        await db[COLLECTION].update_one(
            {"runtime": RUNTIME},
            {"$set": persisted_doc},
            upsert=True,
        )
    except Exception as exc:                                # noqa: BLE001
        logger.warning(f"weight_state persist failed: {exc}")

    logger.info(
        f"weight_state refreshed runtime={RUNTIME} "
        f"resolved={metrics['resolved_count']} "
        f"weights={new_state.as_dict()}"
    )
    return {
        "runtime": RUNTIME,
        "state": new_state.as_dict(),
        "metrics": metrics,
        "refreshed_at": persisted_doc["refreshed_at"].isoformat(),
    }


__all__ = ["COLLECTION", "RUNTIME", "load", "refresh"]
