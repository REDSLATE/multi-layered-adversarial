"""Pipeline dataclasses — the only shapes that cross layer boundaries.

Brain emits BrainOpinion. Pipeline emits PipelineReceipt. Everything
else (SeatVerdict, GovernorModifier, RoadGuardVerdict) is internal to
the orchestrator and lives for one pipeline call.

Field shapes match the operator-supplied spec exactly so downstream
analytics, the /why endpoint, and the UI can rely on stable contracts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Literal


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
RestrictionSource = Literal["brain", "seat", "roadguard", "broker"]


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


@dataclass
class SeatVerdict:
    """Seat layer output. The seat owns ALLOW/BLOCK."""
    decision: Decision
    reason: str
    autonomy_mode: Mode
    notional_usd: float


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
    ts: str = ""
