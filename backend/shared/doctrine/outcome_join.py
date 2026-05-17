"""Outcome-join layer for the doctrine sidecar audit log.

Doctrine (2026-02-17, roadmap step B):
    Packets flow at ingest. Then they need to TEACH.

    When a position closes, find the doctrine_sidecars row joined by
    intent_id and append an `outcome_join` envelope so Shelly + the
    scorecard endpoint can answer questions like:

        * A_QUALITY vs C_QUALITY win-rate by lane
        * Did `governor.block_reasons` correlate with avoided losses?
        * Did `adversary.objections` correlate with failed trades?
        * Did `execution_judge.execution_ready` correlate with lower
          slippage?

    The join is ONE-SHOT, append-only. We never mutate the original
    packet. The outcome envelope lives at the top level of the
    `doctrine_sidecars` row so a reader doesn't have to deep-dive to
    aggregate.

    Lane-neutral. No execution, no decisions — pure data join.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from db import db
from namespaces import DOCTRINE_SIDECARS

logger = logging.getLogger("risedual.doctrine.outcome_join")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def join_outcome_to_doctrine(
    *,
    intent_id: Optional[str],
    position_id: Optional[str],
    lane: Optional[str],
    symbol: Optional[str],
    outcome_label: Optional[str],          # win | loss | scratch | stopped_out
    pnl_usd: Optional[float],
    pnl_pct: Optional[float],
    opened_at: Optional[str],
    closed_at: Optional[str],
    closing_actor: Optional[str],          # e.g. "take_profit_guard"
    mae_usd: Optional[float] = None,       # max adverse excursion
    mfe_usd: Optional[float] = None,       # max favorable excursion
    extra: Optional[dict] = None,
) -> bool:
    """One-shot join of the close outcome onto the doctrine_sidecars row.

    Returns True iff we successfully attached an outcome envelope.
    False on `no intent_id`, `no matching doctrine row`, or any DB
    failure — callers treat False as "not all positions came from an
    intent that had a doctrine packet" (e.g. manual operator opens).
    """
    if not intent_id:
        return False
    try:
        existing = await db[DOCTRINE_SIDECARS].find_one(
            {"intent_id": intent_id}, {"_id": 0, "outcome_join": 1},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("outcome_join lookup failed for intent %s: %s", intent_id, e)
        return False
    if existing is None:
        # The intent never carried a doctrine packet (e.g. ingested
        # before the doctrine layer shipped, or a non-doctrinal path
        # like the legacy `paper-open` endpoint). Nothing to teach.
        # NOTE: `existing` may be `{}` when the row exists but has no
        # `outcome_join` field yet — that's the normal first-close path,
        # so we explicitly check for `None` here, not falsy.
        return False
    if existing.get("outcome_join"):
        # Already joined — don't double-attach. Take-profit + manual
        # operator close on the same position would otherwise race.
        return False

    envelope = {
        "joined_at": _now_iso(),
        "position_id": position_id,
        "lane": lane,
        "symbol": symbol,
        "outcome_label": outcome_label,
        "pnl_usd": float(pnl_usd) if pnl_usd is not None else None,
        "pnl_pct": float(pnl_pct) if pnl_pct is not None else None,
        "opened_at": opened_at,
        "closed_at": closed_at,
        "closing_actor": closing_actor,
        # Excursion fields are placeholders today — the position
        # monitor doesn't yet track tick-by-tick high/low. When it does
        # we'll populate these. Schema lives now so Shelly doesn't have
        # to migrate later.
        "max_adverse_excursion_usd": float(mae_usd) if mae_usd is not None else None,
        "max_favorable_excursion_usd": float(mfe_usd) if mfe_usd is not None else None,
        "extra": dict(extra or {}),
    }

    try:
        await db[DOCTRINE_SIDECARS].update_one(
            {"intent_id": intent_id, "outcome_join": {"$exists": False}},
            {"$set": {"outcome_join": envelope}},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("outcome_join write failed for intent %s: %s", intent_id, e)
        return False
    return True
