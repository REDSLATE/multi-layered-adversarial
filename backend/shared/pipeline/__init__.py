"""Unified execution pipeline.

Operator doctrine (2026-02-20, locked):

    There are ONLY THREE BLOCKERS in this system:
        1. Seat       — declares the brain may not trade for this lane.
        2. RoadGuard  — declares it is unsafe to send any order right now.
        3. Broker     — the external exchange refuses the order.

    Everything else is EVIDENCE. Brains emit opinions. Doctrine emits
    quality grades. Auditors emit objections. Governors emit risk
    multipliers. None of them can block. They all write rows the seat
    can read, but the seat is the only authority on "may this brain
    place an order through this lane at all?"

Public surface:

    run_execution_pipeline(opinion, ...)
        → PipelineReceipt with a single `restriction_source` field
          that answers "who stopped it?" for every intent.

    /api/intents/{intent_id}/why
        → reads the receipt; returns the one-sentence answer.

This module replaces the 20-gate chain in `shared/execution.py`
and `shared/auto_router.py` when `UNIFIED_PIPELINE_ENABLED=true`.
Old chain stays in place behind the flag for 48h rollback safety.
"""
from .models import (
    BrainOpinion,
    GovernorModifier,
    PipelineReceipt,
    RoadGuardVerdict,
    SeatVerdict,
)
from .execution_pipeline import run_execution_pipeline

__all__ = [
    "BrainOpinion",
    "GovernorModifier",
    "PipelineReceipt",
    "RoadGuardVerdict",
    "SeatVerdict",
    "run_execution_pipeline",
]
