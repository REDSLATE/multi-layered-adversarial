"""Pipeline dataclasses — the only shapes that cross layer boundaries.

Brain emits BrainOpinion. Pipeline emits PipelineReceipt. Everything
else (SeatVerdict, GovernorModifier, RoadGuardVerdict) is internal to
the orchestrator and lives for one pipeline call.

Field shapes match the operator-supplied spec exactly so downstream
analytics, the /why endpoint, and the UI can rely on stable contracts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional


Action = Literal["BUY", "SELL", "HOLD", "ABSTAIN"]
Decision = Literal["ALLOW", "BLOCK"]
Mode = Literal["observe", "shadow", "toehold", "auto_execute"]
FinalStatus = Literal[
    "NO_ORDER",            # brain emitted HOLD/ABSTAIN
    "BLOCKED",             # seat or roadguard refused
    "DECISION_LOGGED",     # seat in observe/shadow → no broker
    "SUBMITTED",           # broker accepted
    "BROKER_ERROR",        # broker raised
]
RestrictionSource = Literal["brain", "firewall", "seat", "roadguard", "broker"]


@dataclass
class BrainOpinion:
    """Brain layer output — the only thing a brain hands to the seat."""
    intent_id: str
    brain_id: str
    lane: str
    symbol: str
    action: Action
    confidence: float
    notional_usd: float
    evidence: Dict[str, Any] = field(default_factory=dict)
    # ─── Paradox v3 plan layer (2026-02, Step 5 — threaded through) ──
    # `plan` is the brain's planning artefact; `intent_version` is the
    # envelope discriminator. Adapter (`_opinion_from_intent`) lifts
    # the persisted intent doc via `normalize_intent`, so this field
    # is populated for BOTH v2 and v3 rows. SeatPolicy reads
    # `plan.intent` to detect `WAIT_FOR_TRIGGER` and route to the
    # trigger_watcher queue instead of the broker.
    intent_version: Optional[str] = None
    plan: Optional[Dict[str, Any]] = None
    # `execution` carries the broker-targeted ticket details — for
    # v3 refire (Step 5.b), `execution.limit_price` may be populated
    # so the pipeline dispatches to the adapter's limit-order path
    # instead of market.
    execution: Optional[Dict[str, Any]] = None


@dataclass
class SeatVerdict:
    """Seat layer output. The seat owns ALLOW/BLOCK."""
    decision: Decision
    reason: str
    autonomy_mode: Mode
    notional_usd: float
    # Consensus provenance (2026-06-24). Populated by SeatPolicy.evaluate
    # for both ALLOW and BLOCK paths so the receipt can show "we boosted
    # the floor by +0.10 because Barracuda+Hellcat agreed". None when
    # consensus didn't apply (e.g. HOLD executor, brain-not-current-
    # seat-holder reject before the floor check is reached).
    consensus: Optional[Dict[str, Any]] = None


@dataclass
class GovernorModifier:
    """Governor layer output — modifier only. Cannot block."""
    risk_multiplier: float
    reason: str


@dataclass
class RoadGuardVerdict:
    """RoadGuard layer output. The only post-seat hard stop."""
    passed: bool
    reason: str


@dataclass
class PipelineReceipt:
    """The single receipt every intent produces.

    `restriction_source` answers the operator's question:
    "who stopped this trade?" in one of four canonical buckets.
    """
    intent_id: str
    final_status: FinalStatus
    final_reason: str
    restriction_source: RestrictionSource
    requested_notional: float
    final_notional: float
    broker_called: bool
    # Brain-side context surfaced on the /why endpoint so the operator
    # doesn't have to cross-reference shared_intents to interpret it.
    brain_id: str = ""
    lane: str = ""
    symbol: str = ""
    action: str = ""
    confidence: float = 0.0
    autonomy_mode: str = ""
    governor_multiplier: float = 1.0
    evidence_snapshot: Dict[str, Any] = field(default_factory=dict)
    # Consensus provenance (2026-06-24). Carries the five fields the
    # operator pinned for receipts:
    #   base_confidence, advisor_boost (= delta), effective_confidence,
    #   advisor_votes_used (= agree + disagree, HOLD opinions excluded),
    #   advisor_window_seconds.
    # `applied: bool` is true iff delta != 0 (operator-friendly flag).
    # `agree_brains` and `disagree_brains` are list[str] for the
    # post-mortem detail panel. None on rows where the seat didn't
    # reach the floor check (e.g. brain_not_current_seat_holder
    # rejects).
    consensus: Optional[Dict[str, Any]] = None
    ts: str = ""
