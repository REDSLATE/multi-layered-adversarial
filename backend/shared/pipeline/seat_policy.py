"""Seat policy — the ONLY internal authority that may block.

Reads the existing Paradox v2 collections so operator seat assignments
(made via the Quick Seat Switches UI) flow into the unified pipeline
without re-input.

Block reasons surfaced on PipelineReceipt.final_reason:
  seat_missing                  — no row for this lane's executor seat
  seat_disabled                 — operator flipped seat.enabled = False
  brain_not_trusted_for_seat    — emitting brain not in trust list
  below_seat_confidence_min     — opinion.confidence < seat.confidence_min
"""
from __future__ import annotations

from typing import Any, Dict

from db import db
from namespaces import (
    PARADOX_V2_SEAT_POLICY,
    PARADOX_V2_SEAT_TRUSTED,
)

from .models import BrainOpinion, SeatVerdict


# Lane → executor seat id. Only executor seats place real orders.
# Strategist/Governor/Auditor seats produce evidence; they never gate
# an order on their own.
LANE_TO_EXECUTOR_SEAT: Dict[str, str] = {
    "equity": "equity_executor",
    "crypto": "crypto_executor",
}


class SeatPolicy:
    """Stateless wrapper around `paradox_v2_seat_policy_config` +
    `paradox_v2_seat_trusted_brains`. Constructed once per request."""

    async def evaluate(self, opinion: BrainOpinion) -> SeatVerdict:
        seat_id = LANE_TO_EXECUTOR_SEAT.get(opinion.lane)
        if not seat_id:
            return SeatVerdict(
                decision="BLOCK",
                reason=f"unknown_lane:{opinion.lane}",
                autonomy_mode="observe",
                notional_usd=0.0,
            )

        seat: Dict[str, Any] | None = await db[PARADOX_V2_SEAT_POLICY].find_one(
            {"seat_id": seat_id}, {"_id": 0},
        )
        if not seat:
            return SeatVerdict(
                decision="BLOCK",
                reason="seat_missing",
                autonomy_mode="observe",
                notional_usd=0.0,
            )

        autonomy_mode = str(seat.get("autonomy_mode") or "observe")

        if not seat.get("enabled", False):
            return SeatVerdict(
                decision="BLOCK",
                reason="seat_disabled",
                autonomy_mode=autonomy_mode,
                notional_usd=0.0,
            )

        trust = await db[PARADOX_V2_SEAT_TRUSTED].find_one(
            {"seat_id": seat_id, "brain_id": opinion.brain_id},
            {"_id": 0},
        )
        if not trust:
            return SeatVerdict(
                decision="BLOCK",
                reason=f"brain_not_trusted_for_seat:{opinion.brain_id}->{seat_id}",
                autonomy_mode=autonomy_mode,
                notional_usd=0.0,
            )

        conf_min = float(seat.get("confidence_min", 0.0) or 0.0)
        if opinion.confidence < conf_min:
            return SeatVerdict(
                decision="BLOCK",
                reason=f"below_seat_confidence_min:{opinion.confidence:.3f}<{conf_min:.3f}",
                autonomy_mode=autonomy_mode,
                notional_usd=0.0,
            )

        max_notional = float(seat.get("max_notional_usd", 0.0) or 0.0)
        return SeatVerdict(
            decision="ALLOW",
            reason="seat_policy_passed",
            autonomy_mode=autonomy_mode,
            notional_usd=max_notional,
        )
