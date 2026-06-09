"""Audit-only observer for position-side misreads.

Doctrine (2026-06-09, operator-set): "Leave the live trading path
untouched for the moment. Use the new classifier as an OBSERVER
until you have a few days of evidence." This module installs the
observer; it does not modify any routing decision.

Wiring approach:
  * Brain emits intent (BUY/SELL).
  * Intent ingest writes it to `shared_intents` as today.
  * A separate periodic poller (`audit_recent_intents_for_misreads`)
    scans newly-ingested intents, fetches the live broker position
    for each (lane, symbol), compares the brain's implicit
    assumption (FLAT, since brains have no inventory awareness
    yet) against broker reality, and writes a row to
    `shared_position_misreads` when they diverge.
  * NO intent is modified. NO order is blocked. The auto-router
    runs exactly as it does today.

Why this design:
  * Lets the operator collect "a few days of evidence" with zero
    risk of regressing the trading loop.
  * If misread volume is 0-2/day → AAPL was an isolated incident,
    no execution rewire needed.
  * If misread volume is 20-50/day → systemic — operator now has
    the evidence to justify the deeper position-aware sizing fix.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from shared.position_model import (
    ACTION_BUY, ACTION_SELL,
    MISREAD_COLLECTION, PositionMisread, PositionSide,
    PositionState, detect_misread,
)

logger = logging.getLogger(__name__)

# Async broker fetch signature: given (lane, symbol) → PositionState
PositionFetcher = Callable[[str, str], Awaitable[Optional[PositionState]]]


async def audit_one_intent(
    intent: dict,
    db,
    position_fetcher: PositionFetcher,
) -> Optional[PositionMisread]:
    """Audit a single intent. Returns a `PositionMisread` row if the
    brain's assumption (FLAT — the only assumption brains can make
    today, no inventory awareness) disagrees with broker reality.

    Pure observer: writes to `shared_position_misreads`, never
    mutates the intent or affects routing. Safe to call from any
    code path, in any order. All exceptions are swallowed so a
    misread-audit failure cannot impact trading.
    """
    try:
        action = (intent.get("action") or "").upper()
        if action not in (ACTION_BUY, ACTION_SELL):
            return None
        symbol = intent.get("symbol")
        lane = intent.get("lane") or "equity"
        if not symbol:
            return None

        actual = await position_fetcher(lane, symbol)
        if actual is None:
            # Broker fetch unavailable — skip silently. Better to
            # under-audit than to clutter audit log with noise rows.
            return None

        # Today's brains have NO position-state awareness, so the
        # assumed_side is FLAT for every intent. When the broker
        # actually holds a position, that's a misread. Once the
        # brain emits its own assumption in the intent payload
        # (future work), swap this default for `intent.get(
        # "assumed_position_side", "flat")`.
        assumed_side = PositionSide(
            (intent.get("assumed_position_side") or "flat").lower()
        )

        intended_qty = float(
            intent.get("size_usd")
            or intent.get("notional_usd")
            or intent.get("dry_run_notional_usd")
            or 1.0
        )
        if intended_qty <= 0:
            intended_qty = 1.0  # symbolic — classifier just needs sign

        misread = detect_misread(
            emitted_action=action,
            assumed_side=assumed_side,
            actual=actual,
            brain=(intent.get("stack") or intent.get("brain") or "unknown"),
            lane=lane,
            intended_qty=intended_qty,
            note=(
                f"intent_id={intent.get('intent_id','?')} "
                f"setup={intent.get('setup_score','?')} "
                f"observer-only audit"
            ),
        )
        if misread is None:
            return None

        doc = misread.to_doc()
        doc["intent_id"] = intent.get("intent_id")
        doc["ingest_ts"] = intent.get("ingest_ts")
        await db[MISREAD_COLLECTION].insert_one(doc)
        return misread

    except Exception as e:  # noqa: BLE001
        # Audit failures NEVER bubble — observer must be safe by
        # construction. Log only.
        logger.warning(
            "position_misread_audit failed for intent=%s: %s",
            (intent.get("intent_id") if isinstance(intent, dict) else "?"), e,
        )
        return None


async def list_recent_misreads(db, limit: int = 20) -> list[dict]:
    """Last N misreads, newest first. Strips Mongo `_id` for clean
    JSON return to the UI."""
    rows = await db[MISREAD_COLLECTION].find({}, {"_id": 0}).sort(
        "detected_at", -1,
    ).to_list(int(limit))
    return rows


async def misread_summary_24h(db) -> dict:
    """Single-number heuristic the operator asked for: how many
    misreads in the last 24h?
        0-2  → AAPL was isolated
        20-50 → systemic position-state problem
    """
    from datetime import timedelta
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=24)
    ).isoformat()
    pipeline = [
        {"$match": {"detected_at": {"$gte": cutoff}}},
        {"$group": {
            "_id": "$kind",
            "count": {"$sum": 1},
            "missed_short_profit_count": {
                "$sum": {"$cond": ["$missed_short_profit", 1, 0]},
            },
        }},
    ]
    rows = await db[MISREAD_COLLECTION].aggregate(pipeline).to_list(10)
    total = sum(r["count"] for r in rows)
    short_profit = sum(r["missed_short_profit_count"] for r in rows)
    if total == 0:
        verdict = "no_misreads_in_24h"
    elif total <= 2:
        verdict = "isolated_likely_aapl_only"
    elif total <= 10:
        verdict = "monitor — small but recurring"
    elif total <= 50:
        verdict = "systemic — position-state model needs the fix"
    else:
        verdict = "critical — every batch is misreading positions"
    return {
        "since": cutoff,
        "total": total,
        "missed_short_profit": short_profit,
        "verdict": verdict,
        "by_kind": rows,
    }
