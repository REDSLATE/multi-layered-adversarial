"""Advisor opinions store — window-scoped DB layer.

Replaces "non-seat-holder intents are skipped" with "non-seat-holder
intents are stored as advisor opinions, and the seat holder
synthesizes them via consensus" (operator pin, 2026-02-23).

Storage:
    Mongo collection `advisor_opinions`. One doc per emitted intent
    that wasn't from the executor seat holder. Each doc carries the
    minimum fields the consensus engine needs plus an `expires_at`
    timestamp for the window-scoped TTL.

API:
    `store_opinion(db, intent)`             — write one opinion
    `collect_for(db, symbol, lane, window)` — return opinions in window
    `ensure_indexes(db)`                    — create the (symbol,lane) +
                                              TTL indexes on startup

NO consensus math lives here — `consensus_engine.build_consensus` is
the sole entry point for synthesis. This module is pure storage.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from shared.consensus import BrainOpinion


logger = logging.getLogger("risedual.consensus.store")

COLLECTION = "advisor_opinions"
DEFAULT_WINDOW_SEC = 60
DEFAULT_TTL_SEC = 5 * 60  # records survive 5min beyond emit (audit)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_indexes(db) -> None:
    """Idempotent index setup. Safe to call at startup every boot."""
    try:
        await db[COLLECTION].create_index([
            ("symbol", 1), ("lane", 1), ("emitted_at", -1),
        ])
        # Mongo TTL — auto-purge rows past `expires_at` so the
        # collection doesn't grow unbounded across days.
        await db[COLLECTION].create_index(
            "expires_at",
            expireAfterSeconds=0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("advisor_opinions index ensure failed: %r", exc)


async def store_opinion(db, intent: dict[str, Any]) -> str | None:
    """Persist one brain's opinion derived from an emitted intent.

    Returns the inserted opinion id, or None if the intent is missing
    required fields (we never raise — opinion storage is best-effort
    auditing and must NEVER take down the ingest path).
    """
    try:
        brain = (intent.get("stack_canonical") or intent.get("stack") or "").lower()
        symbol = intent.get("symbol")
        lane = intent.get("lane")
        action = intent.get("action") or "HOLD"
        confidence = float(intent.get("confidence") or 0.0)
        if not brain or not symbol or not lane:
            return None
        now = _now()
        doc = {
            "brain": brain,
            "symbol": symbol,
            "lane": lane,
            "action": action,
            "confidence": confidence,
            "edge": float(intent.get("edge") or 0.0),
            "reason": intent.get("rationale") or "",
            "intent_id": intent.get("intent_id"),
            "market_regime": (
                (intent.get("evidence") or {}).get("market_regime")
                or (intent.get("regime") or None)
            ),
            "emitted_at": now,
            "expires_at": now + timedelta(seconds=DEFAULT_TTL_SEC),
        }
        result = await db[COLLECTION].insert_one(doc)
        return str(result.inserted_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "store_opinion failed for intent_id=%s: %r",
            intent.get("intent_id"), exc,
        )
        return None


async def collect_for(
    db,
    symbol: str,
    lane: str,
    window_sec: int = DEFAULT_WINDOW_SEC,
) -> list[BrainOpinion]:
    """Return advisor opinions for `(symbol, lane)` in the last
    `window_sec` seconds, newest first per brain (one most-recent
    opinion per brain — the seat doesn't want stale duplicates from
    a chatty brain inflating its vote).
    """
    cutoff = _now() - timedelta(seconds=window_sec)
    cursor = db[COLLECTION].find(
        {
            "symbol": symbol,
            "lane": lane,
            "emitted_at": {"$gte": cutoff},
        },
        {"_id": 0},
        sort=[("emitted_at", -1)],
    )
    seen: set[str] = set()
    out: list[BrainOpinion] = []
    async for row in cursor:
        brain = row.get("brain")
        if not brain or brain in seen:
            continue
        seen.add(brain)
        emitted_at = row.get("emitted_at")
        out.append(BrainOpinion(
            brain=brain,
            symbol=row.get("symbol", symbol),
            lane=row.get("lane", lane),
            action=row.get("action") or "HOLD",
            confidence=float(row.get("confidence") or 0.0),
            edge=float(row.get("edge") or 0.0),
            reason=row.get("reason") or "",
            intent_id=row.get("intent_id"),
            market_regime=row.get("market_regime"),
            emitted_at=(
                emitted_at.isoformat()
                if isinstance(emitted_at, datetime) else emitted_at
            ),
        ))
    return out


__all__ = [
    "COLLECTION", "DEFAULT_WINDOW_SEC",
    "ensure_indexes", "store_opinion", "collect_for",
]
