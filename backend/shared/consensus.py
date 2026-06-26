"""Consensus dataclasses — wire-protocol for the advisor model.

Operator pin (2026-02-23):
    Brains advise.
    Paradox synthesizes.
    Seat authorizes.
    Governor sizes.
    RoadGuard blocks danger.
    Broker executes.

A brain emitting an intent is the SAME thing as the brain stating an
OPINION — `BrainOpinion` is just the canonical name for that opinion
in the consensus layer. Existing intents in `shared_intents` are
written into a window-scoped `advisor_opinions` collection so the
seat holder can synthesize a `ConsensusIntent` from the last N
seconds of opinions per (symbol, lane).

This module owns ONLY the data shapes. No I/O, no engine logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


Action = Literal["BUY", "SELL", "SHORT", "COVER", "HOLD", "ABSTAIN"]


@dataclass(frozen=True)
class BrainOpinion:
    """One brain's view on one symbol at one moment in time.

    `edge` is an optional signed score (e.g., a strategy's raw alpha
    estimate before confidence calibration). Unused by the v1 engine
    but plumbed through for later regime-aware tie-breaking.
    """
    brain: str          # canonical brain id (barracuda / gto / camino / hellcat)
    symbol: str
    lane: str           # "equity" | "crypto"
    action: Action
    confidence: float   # 0..1
    edge: float = 0.0
    reason: str = ""
    intent_id: str | None = None       # link back to the source intent
    market_regime: str | None = None    # regime tag observed at emit time
    emitted_at: str | None = None       # ISO timestamp


@dataclass(frozen=True)
class ConsensusIntent:
    """Single decision synthesized by the seat from advisor opinions."""
    symbol: str
    lane: str
    action: Action
    confidence: float
    agreed_brains: list[str] = field(default_factory=list)
    disagreed_brains: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


__all__ = ["Action", "BrainOpinion", "ConsensusIntent"]
