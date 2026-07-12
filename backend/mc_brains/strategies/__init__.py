"""Strategy protocol — the interface every P7 pulse strategy honors.

Doctrine (2026-07-12 P7a): each of the four brains gets its own
strategy. Strategies are:

  1. PURE — no DB reads, no wall-clock, no cross-brain state.
     Same snapshot → same StrategyResult. Deterministic.
  2. FAST — synchronous, sub-millisecond. The pulse loop fires
     every 15s across dozens of symbols; strategies must not be
     a bottleneck.
  3. SELF-IDENTIFYING — every reason code carries a family prefix
     that identifies the emitting brain (TREND_, MOMENTUM_, MEAN_,
     EXEC_). Downstream audit rows and the distinctness metric
     both key on this to prove separation.
  4. HONESTLY DISAGREEING — nothing about the strategy design
     requires disagreement for its own sake. If evidence is
     overwhelming, all four strategies SHOULD agree. What must
     be distinct is the REASONING PATH.

The confidence returned is the RAW confidence (pre-personality
clamp). The personality multiplier is applied by
`PulseBrain` AFTER the strategy runs — that
way personality remains a confidence modulator only, never a
strategy input.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Protocol, runtime_checkable

from mc_pulse.snapshot import MarketSnapshot


@dataclass(frozen=True)
class StrategyResult:
    """Output contract for every P7 strategy.

    `action`: "BUY" | "SELL" | "HOLD" — the strategy's directional call.
    `confidence`: 0..1, raw (pre-personality). 0.0 = INSUFFICIENT_DATA
        equivalent. 1.0 = maximum conviction the strategy can express.
    `reason_codes`: tuple of UPPER_SNAKE_CASE strings. FIRST code MUST
        carry the strategy's family prefix (TREND_/MOMENTUM_/MEAN_/EXEC_)
        so downstream audits can identify the emitting mind.
    `edge_evidence`: optional debug dict for the rationale string.
        Keys are lowercase snake_case; values are simple types.
    """
    action: str
    confidence: float
    reason_codes: tuple[str, ...] = ()
    edge_evidence: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.action not in ("BUY", "SELL", "HOLD"):
            raise ValueError(
                f"StrategyResult.action must be BUY/SELL/HOLD, got {self.action!r}"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"StrategyResult.confidence out of range [0,1]: {self.confidence}"
            )


@runtime_checkable
class Strategy(Protocol):
    """Every strategy declares its reason-code family and an
    `evaluate` method. The pulse brain wires this in place of the
    legacy NeutralAdversarialBrain core once P7a lands."""

    reason_code_family: ClassVar[str]

    def evaluate(self, snapshot: MarketSnapshot) -> StrategyResult:
        ...
