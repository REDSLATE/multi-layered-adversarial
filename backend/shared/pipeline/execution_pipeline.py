"""The orchestrator. Matches the operator-supplied spec verbatim.

Flow:
    1. Brain HOLD/ABSTAIN → NO_ORDER receipt (brain is not a blocker;
       it just chose not to act).
    2. Seat evaluates the opinion. If BLOCK → BLOCKED receipt with
       restriction_source="seat". This is the FIRST real authority.
    3. Governor produces a risk_multiplier (cannot block).
    4. Pipeline computes final_notional = min(seat.cap, requested) * mult.
    5. RoadGuard checks final_notional for binary safety. If not passed
       → BLOCKED receipt with restriction_source="roadguard".
    6. If seat.autonomy_mode in {observe, shadow} → DECISION_LOGGED
       receipt (no broker call). This is the canonical "learn without
       risk" path.
    7. If toehold → cap notional at seat.notional_usd (the seat
       publishes a toehold-sized cap).
    8. Broker submit. SUBMITTED on success, BROKER_ERROR on exception.

Every branch writes exactly ONE receipt to `pipeline_receipts`.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone

from .governor import Governor
from .models import BrainOpinion, PipelineReceipt
from .receipts import ReceiptStore
from .roadguard import RoadGuard
from .seat_policy import SeatPolicy


logger = logging.getLogger("pipeline.execution")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def run_execution_pipeline(
    opinion: BrainOpinion,
    *,
    seat_policy: SeatPolicy,
    governor: Governor,
    roadguard: RoadGuard,
    broker,
    receipt_store: ReceiptStore,
) -> PipelineReceipt:
    """Run the unified pipeline and persist one receipt.

    `broker` must expose:
        async submit_market_order(symbol, side, notional_usd, lane) -> dict
    Exceptions raised by broker are caught and recorded; they do NOT
    propagate. The receipt is always written.
    """
    base = {
        "intent_id": opinion.intent_id,
        "brain_id": opinion.brain_id,
        "lane": opinion.lane,
        "symbol": opinion.symbol,
        "action": opinion.action,
        "confidence": opinion.confidence,
        "requested_notional": opinion.notional_usd,
        "evidence_snapshot": dict(opinion.evidence or {}),
        "ts": _now(),
    }

    # 1. Brain cannot block. HOLD/ABSTAIN → no-order receipt.
    # ─── Paradox v3 exception (Step 5) ────────────────────────────
    # A v3 WAIT_FOR_TRIGGER / WAIT_CONFIRMATION plan carries
    # `action="HOLD"` (per §6.2 mapping — execution.action is null on
    # the wait state) but is NOT a brain-side abstain. The seat must
    # see the plan so it can park it on the watch queue. Bypass the
    # HOLD short-circuit for those two intent values only.
    _plan = opinion.plan or {}
    _is_v3_wait = (
        (opinion.intent_version == "v3")
        and (_plan.get("intent") or "").upper()
        in {"WAIT_FOR_TRIGGER", "WAIT_CONFIRMATION"}
    )
    if opinion.action in ("HOLD", "ABSTAIN") and not _is_v3_wait:
        receipt = PipelineReceipt(
            **base,
            final_status="NO_ORDER",
            final_reason=f"brain_{opinion.action.lower()}",
            restriction_source="brain",
            final_notional=0.0,
            broker_called=False,
            autonomy_mode="",
            governor_multiplier=1.0,
        )
        await receipt_store.write(receipt)
        return receipt

    # 2. Seat is the first real authority.
    seat = await seat_policy.evaluate(opinion)
    if seat.decision == "BLOCK":
        receipt = PipelineReceipt(
            **base,
            final_status="BLOCKED",
            final_reason=seat.reason,
            restriction_source="seat",
            final_notional=0.0,
            broker_called=False,
            autonomy_mode=seat.autonomy_mode,
            governor_multiplier=1.0,
            consensus=seat.consensus,
        )
        await receipt_store.write(receipt)
        return receipt

    # 3. Governor modifies. Cannot block.
    mod = await governor.modify(opinion)

    # 4. Notional assembly. Seat-capped, then governor-scaled.
    final_notional = max(
        0.0,
        min(seat.notional_usd, opinion.notional_usd) * mod.risk_multiplier,
    )

    # 5. RoadGuard hard stop.
    # ──────────────────────────────────────────────────────────────
    # Doctrine pin (2026-06-24 operator guardrail): consensus advisor
    # boost CANNOT bypass RoadGuard. RoadGuard checks
    # trading_controls_disabled / zero_notional / market_closed /
    # insufficient_buying_power / duplicate_order — NONE of these
    # consume `confidence`. The advisor boost only moves the seat's
    # `confidence_min` floor check. A boosted-past-floor intent still
    # has to clear every RoadGuard stop on its own merit.
    # See test_roadguard_unaffected_by_consensus_boost_2026_06_24.py
    # for the explicit regression that pins this.
    road = await roadguard.check(opinion, final_notional)
    if not road.passed:
        receipt = PipelineReceipt(
            **base,
            final_status="BLOCKED",
            final_reason=road.reason,
            restriction_source="roadguard",
            final_notional=0.0,
            broker_called=False,
            autonomy_mode=seat.autonomy_mode,
            governor_multiplier=mod.risk_multiplier,
            consensus=seat.consensus,
        )
        await receipt_store.write(receipt)
        return receipt

    # 6. Observe / shadow → log decision, do not submit.
    if seat.autonomy_mode in ("observe", "shadow"):
        receipt = PipelineReceipt(
            **base,
            final_status="DECISION_LOGGED",
            final_reason=seat.autonomy_mode,
            restriction_source="seat",
            final_notional=final_notional,
            broker_called=False,
            autonomy_mode=seat.autonomy_mode,
            governor_multiplier=mod.risk_multiplier,
            consensus=seat.consensus,
        )
        await receipt_store.write(receipt)
        return receipt

    # 7. Toehold shrinks size to the seat-published toehold cap.
    if seat.autonomy_mode == "toehold":
        final_notional = min(final_notional, seat.notional_usd)

    # 8. Broker submit.
    broker_called = True
    try:
        broker_receipt = await broker.submit_market_order(
            symbol=opinion.symbol,
            side=opinion.action,
            notional_usd=final_notional,
            lane=opinion.lane,
        )
        status = (broker_receipt or {}).get("status", "submitted")
        receipt = PipelineReceipt(
            **base,
            final_status="SUBMITTED",
            final_reason=str(status),
            restriction_source="broker",
            final_notional=final_notional,
            broker_called=broker_called,
            autonomy_mode=seat.autonomy_mode,
            governor_multiplier=mod.risk_multiplier,
            consensus=seat.consensus,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "pipeline broker submit raised intent_id=%s symbol=%s lane=%s",
            opinion.intent_id, opinion.symbol, opinion.lane,
        )
        receipt = PipelineReceipt(
            **base,
            final_status="BROKER_ERROR",
            final_reason=str(exc)[:200],
            restriction_source="broker",
            final_notional=final_notional,
            broker_called=broker_called,
            autonomy_mode=seat.autonomy_mode,
            governor_multiplier=mod.risk_multiplier,
            consensus=seat.consensus,
        )

    await receipt_store.write(receipt)
    return receipt
