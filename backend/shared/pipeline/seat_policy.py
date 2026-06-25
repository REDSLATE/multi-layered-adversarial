"""Seat policy — the ONLY internal authority that may block.

Reads the existing Paradox v2 collections so operator seat assignments
(made via the Quick Seat Switches UI) flow into the unified pipeline
without re-input.

Block reasons surfaced on PipelineReceipt.final_reason:
  seat_missing                  — no row for this lane's executor seat
  seat_disabled                 — operator flipped seat.enabled = False
  brain_not_current_seat_holder — emitting brain is NOT the operator's
                                  current pick in `brain_roster` (the
                                  authoritative QSS surface). Trust
                                  list is only a soft floor; the
                                  roster is the hard authority.
  brain_not_trusted_for_seat    — emitting brain not in trust list
                                  (defensive — kept as a second line
                                  of defense in case the trust seed
                                  drifts ahead of a roster swap)
  below_seat_confidence_min     — opinion.confidence < seat.confidence_min

History (2026-06-19): added the current-seat-holder check after a
Prod incident where Camino executed an equity SELL while Barracuda
was the operator's pinned equity executor. Trust list still allowed
Camino (legacy `roster_assign_mirror` rows accumulate but never
revoke). The roster check closes that gap.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from db import db
from namespaces import (
    BRAIN_ROSTER,
    PARADOX_V2_SEAT_POLICY,
    PARADOX_V2_SEAT_TRUSTED,
)

from .models import BrainOpinion, SeatVerdict
from .consensus_pool import (
    compute_consensus_boost,
    record_advisory_opinion,
    record_telemetry,
)
from .trigger_watcher import enqueue_watch_plan  # 2026-02 Paradox v3 (Step 5)
from shared.intent_envelope_v3 import HORIZON_TTL_DEFAULTS  # ttl fallback table


# Lane → executor seat id. Only executor seats place real orders.
# Strategist/Governor/Auditor seats produce evidence; they never gate
# an order on their own.
LANE_TO_EXECUTOR_SEAT: Dict[str, str] = {
    "equity": "equity_executor",
    "crypto": "crypto_executor",
}

# Paradox v2 seat_id → canonical `brain_roster.assignments` key.
# Bridges the two layers — the Paradox v2 trust/config uses
# `equity_executor`/`crypto_executor`, while the canonical 8-seat
# roster (which the operator's QSS panel writes) uses `executor` for
# the equity executor and `crypto` for the crypto executor.
SEAT_ID_TO_ROSTER_KEY: Dict[str, str] = {
    "equity_executor": "executor",
    "crypto_executor": "crypto",
}


async def _current_roster_holder(seat_id: str) -> Optional[str]:
    """Return the brain currently pinned to the executor seat for this
    lane in the canonical roster, or None if the slot is vacant.

    This is the authoritative answer to "who may fire orders for this
    lane right now?" — drives the hard `brain_not_current_seat_holder`
    block in SeatPolicy.evaluate(). Read-only; never writes."""
    roster_key = SEAT_ID_TO_ROSTER_KEY.get(seat_id)
    if not roster_key:
        return None
    doc = await db[BRAIN_ROSTER].find_one(
        {"_id": "current"}, {"_id": 0, "assignments": 1},
    )
    if not doc:
        return None
    return ((doc.get("assignments") or {}).get(roster_key)) or None


class SeatPolicy:
    """Stateless wrapper around `paradox_v2_seat_policy_config` +
    `paradox_v2_seat_trusted_brains` + `brain_roster`. Constructed
    once per request."""

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

        # 🔒 Hard authority check — the operator's CURRENT pick in
        # `brain_roster` is the only brain that may fire orders for
        # this lane's executor seat. Trust list is only a soft floor
        # (kept as a defensive second check below). This was added
        # 2026-06-19 after Camino executed a Prod equity SELL while
        # Barracuda was the operator's pinned equity executor — the
        # legacy `roster_assign_mirror` trust entries accumulate but
        # never revoke, so SeatPolicy needed a roster-aware gate.
        current_holder = await _current_roster_holder(seat_id)
        if not current_holder:
            return SeatVerdict(
                decision="BLOCK",
                reason=f"executor_seat_vacant:{seat_id}",
                autonomy_mode=autonomy_mode,
                notional_usd=0.0,
            )
        if current_holder != opinion.brain_id:
            # Non-executor brain. Block as before (fire authority is
            # unchanged) BUT capture the opinion into the consensus
            # pool so the actual executor's confidence_min check can
            # incorporate the non-executor's agreement/disagreement.
            # Doctrine pin (2026-06-24): the operator is keeping the
            # 4-seat structure; consensus boost lets all 4 brains
            # contribute analytically without granting fire authority
            # to anyone but the pinned executor.
            block_reason = (
                f"brain_not_current_seat_holder:"
                f"{opinion.brain_id}!={current_holder}@{seat_id}"
            )
            await record_advisory_opinion(opinion, block_reason)
            return SeatVerdict(
                decision="BLOCK",
                reason=block_reason,
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

        # ─── Paradox v3 WAIT_FOR_TRIGGER short-circuit (Step 5) ─────
        # A v3 plan that declares WAIT_FOR_TRIGGER (or its sibling
        # WAIT_CONFIRMATION) intentionally PARKS the intent — the
        # brain has identified a setup but the trigger hasn't fired.
        # SeatPolicy must NOT route this to the broker. Instead:
        #   * Enqueue the plan onto `intent_watch_queue` (the
        #     trigger_watcher tick will fire/invalidate/expire it).
        #   * Stamp `gate_state="waiting_for_trigger"` on the intent.
        #   * Return BLOCK with reason `paradox_v3_waiting_for_trigger`
        #     so the funnel naturally buckets it at Stage 1 (operator
        #     decision 5C — WAIT plans bucket under seat-blocked).
        # Confidence check is DEFERRED — when the trigger fires and
        # the watcher re-injects, the seat re-evaluates against
        # conf_min at that moment with the live consensus pool.
        wait_verdict = await _maybe_park_v3_wait_plan(opinion, autonomy_mode)
        if wait_verdict is not None:
            return wait_verdict

        conf_min = float(seat.get("confidence_min", 0.0) or 0.0)

        # ── Consensus boost ─────────────────────────────────────────
        # The executor for this (lane, symbol) is now identified.
        # Read the consensus pool (15-min window) and shift
        # `confidence` by ±0.05 per agreeing/disagreeing non-executor
        # advisor, capped at ±0.15. The shifted value is what we
        # actually compare against the floor. See
        # `shared/pipeline/consensus_pool.py` for the full doctrine.
        consensus = await compute_consensus_boost(opinion)
        effective_conf = consensus.effective_confidence
        await record_telemetry(
            opinion.intent_id, consensus, applied=(consensus.delta != 0.0)
        )

        if effective_conf < conf_min:
            return SeatVerdict(
                decision="BLOCK",
                reason=(
                    f"below_seat_confidence_min:"
                    f"{effective_conf:.3f}<{conf_min:.3f}"
                    + (
                        f" (base {opinion.confidence:.3f} "
                        f"{'+' if consensus.advisor_boost >= 0 else ''}{consensus.advisor_boost:.3f} "
                        f"consensus: {consensus.agree_count}↑/{consensus.disagree_count}↓)"
                        if consensus.advisor_count > 0
                        else ""
                    )
                ),
                autonomy_mode=autonomy_mode,
                notional_usd=0.0,
                consensus=consensus.to_dict(),
            )

        max_notional = float(seat.get("max_notional_usd", 0.0) or 0.0)
        # Encode the boost into the verdict reason so the operator
        # can see at a glance whether consensus moved the floor.
        if consensus.advisor_count > 0:
            consensus_suffix = (
                f" (consensus {consensus.agree_count}↑/"
                f"{consensus.disagree_count}↓ "
                f"Δ{'+' if consensus.advisor_boost >= 0 else ''}{consensus.advisor_boost:.3f})"
            )
        else:
            consensus_suffix = ""
        return SeatVerdict(
            decision="ALLOW",
            reason="seat_policy_passed" + consensus_suffix,
            autonomy_mode=autonomy_mode,
            notional_usd=max_notional,
            consensus=consensus.to_dict(),
        )


# ── Paradox v3 WAIT-plan parking helper (Step 5) ────────────────────
_WAIT_PLAN_INTENTS = {"WAIT_FOR_TRIGGER", "WAIT_CONFIRMATION"}


async def _maybe_park_v3_wait_plan(
    opinion: BrainOpinion, autonomy_mode: str,
) -> Optional[SeatVerdict]:
    """If `opinion` is a v3 WAIT_FOR_TRIGGER or WAIT_CONFIRMATION plan,
    park it on `intent_watch_queue` and return a BLOCK verdict so the
    pipeline writes a terminal receipt. Otherwise return None so the
    normal seat-confidence path proceeds.

    The intent's `gate_state` is stamped to `waiting_for_trigger` by
    `enqueue_watch_plan` so the funnel + post-mortem read consistently.

    Confidence floor is NOT applied here — the brain has explicitly
    requested patience; the seat re-evaluates conf_min when the
    trigger fires and the watcher re-injects. (See PRD §5.2.)
    """
    plan = opinion.plan or {}
    intent_name = (plan.get("intent") or "").upper()
    if intent_name not in _WAIT_PLAN_INTENTS:
        return None

    # Compute expiry from `plan.ttl_seconds` (operator-shipped) or
    # fall back to the horizon-derived default per operator 4A.
    from datetime import datetime, timezone, timedelta
    ttl = plan.get("ttl_seconds")
    if ttl is None:
        horizon = (plan.get("horizon") or "UNKNOWN").upper()
        ttl = HORIZON_TTL_DEFAULTS.get(horizon)
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=int(ttl))
        if ttl else None
    )

    await enqueue_watch_plan(
        intent_id=opinion.intent_id,
        symbol=opinion.symbol,
        lane=opinion.lane,
        stance=(plan.get("stance") or "NEUTRAL").upper(),
        trigger_price=plan.get("trigger_price"),
        invalidation_price=plan.get("invalidation_price"),
        expires_at=expires_at,
    )
    return SeatVerdict(
        decision="BLOCK",
        reason=(
            f"paradox_v3_{intent_name.lower()}"
            f":trigger={plan.get('trigger_price')}"
            f",inv={plan.get('invalidation_price')}"
            f",ttl={ttl}s"
        ),
        autonomy_mode=autonomy_mode,
        notional_usd=0.0,
    )
