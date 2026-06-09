"""Neutral adversarial brain core.

Position-neutral by design: this module emits ranked action
hypotheses {BUY, SELL, HOLD, OBSERVE} for a given (symbol,
snapshot). It carries NO knowledge of which seat (strategist /
executor / governor / auditor) it currently holds. The seat is
operator-rotatable via MC's roster, and the seat's policy decides
how the brain's output is treated — never the brain's identity.

This is the stand-in template the operator is running until the
real per-brain wild_adaptive_core_v2 modules are migrated to this
stack. Each running sidecar instantiates one of these with a
distinct `brain_id` and `display_name`. The canonical IDs used
across the whole stack are: alpha, camaro, chevelle, redeye.
The legacy car-template labels (Camino/Barracuda/Hellcat/GTO)
were retired 2026-06-XX in favour of those canonical names.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal
import math
import uuid


Action = Literal["BUY", "SELL", "HOLD", "OBSERVE"]


@dataclass
class Hypothesis:
    name: str
    action: Action
    score: float
    confidence: float
    reasons: List[str]


@dataclass
class BrainIntent:
    intent_id: str
    brain_id: str
    display_name: str
    lane: str
    symbol: str
    action: Action
    confidence: float
    size: float
    shadow_only: bool
    created_at: str
    hypothesis_scores: Dict[str, float]
    reasoning: List[str]
    snapshot: Dict[str, Any]
    memory_tags: List[str]


class NeutralAdversarialBrain:
    """Position-neutral adversarial decision brain.

    The brain only ranks action hypotheses. It does NOT know which
    seat it holds — operator rotation through MC's roster decides
    whether this brain's output becomes a strategist proposal, an
    executor sizing, a governor modulation, or an auditor objection.
    """

    def __init__(
        self,
        brain_id: str,
        display_name: str,
        lane: str = "crypto",
        shadow_only: bool = True,
        min_commitment: float = 0.58,
        min_gap: float = 0.06,
        max_shadow_size: float = 0.0,
    ):
        self.brain_id = brain_id
        self.display_name = display_name
        self.lane = lane
        self.shadow_only = shadow_only
        self.min_commitment = min_commitment
        self.min_gap = min_gap
        self.max_shadow_size = max_shadow_size

    def evaluate(self, symbol: str, snapshot: Dict[str, Any]) -> BrainIntent:
        hypotheses = self._build_hypotheses(snapshot)
        winner, runner_up = sorted(
            hypotheses, key=lambda h: h.score, reverse=True,
        )[:2]
        gap = winner.score - runner_up.score

        reasoning: List[str] = [
            f"display_name={self.display_name} brain_id={self.brain_id} lane={self.lane}",
        ]
        for h in hypotheses:
            reasoning.append(
                f"{h.name}: action={h.action}, score={h.score:.3f}",
            )
            reasoning.extend(f"  - {r}" for r in h.reasons)
        reasoning.append(f"Top hypothesis: {winner.name}")
        reasoning.append(f"Score gap: {gap:.3f}")

        if winner.score < self.min_commitment:
            final_action: Action = "OBSERVE"
            final_confidence = winner.score
            reasoning.append(
                f"Commitment below floor {self.min_commitment:.2f}; emitting OBSERVE.",
            )
        elif gap < self.min_gap:
            final_action = "HOLD"
            final_confidence = winner.score
            reasoning.append(
                f"Conflict gap below {self.min_gap:.2f}; emitting HOLD.",
            )
        else:
            final_action = winner.action
            final_confidence = winner.score
            reasoning.append(f"Resolved to {final_action}.")

        size = (
            0.0 if self.shadow_only
            else self._size_from_confidence(final_confidence)
        )

        return BrainIntent(
            intent_id=str(uuid.uuid4()),
            brain_id=self.brain_id,
            display_name=self.display_name,
            lane=self.lane,
            symbol=symbol,
            action=final_action,
            confidence=round(float(final_confidence), 4),
            size=round(float(size), 4),
            shadow_only=self.shadow_only,
            created_at=datetime.now(timezone.utc).isoformat(),
            hypothesis_scores={
                h.name: round(float(h.score), 4) for h in hypotheses
            },
            reasoning=reasoning,
            snapshot=snapshot,
            memory_tags=self._memory_tags(
                snapshot, final_action, final_confidence,
            ),
        )

    def _build_hypotheses(self, s: Dict[str, Any]) -> List[Hypothesis]:
        price_change = float(s.get("price_change_pct", 0.0))
        volume_change = float(s.get("volume_change_pct", 0.0))
        rsi = float(s.get("rsi", 50.0))
        spread_bps = float(s.get("spread_bps", 9999.0))
        volatility = float(s.get("volatility", 0.0))
        trend = float(s.get("trend_score", 0.0))
        liquidity = float(s.get("liquidity_score", 0.5))

        buy_score = self._clamp(
            0.50
            + trend * 0.18
            + price_change * 0.03
            + volume_change * 0.01
            + (50.0 - rsi) * 0.004
            + liquidity * 0.08
            - volatility * 0.10
            - spread_bps * 0.001
        )
        sell_score = self._clamp(
            0.50
            - trend * 0.18
            - price_change * 0.03
            + volume_change * 0.008
            + (rsi - 50.0) * 0.004
            + liquidity * 0.05
            - volatility * 0.08
            - spread_bps * 0.001
        )
        hold_score = self._clamp(
            0.45
            + volatility * 0.18
            + spread_bps * 0.002
            + (1.0 - liquidity) * 0.15
            - abs(trend) * 0.08
        )
        observe_score = self._clamp(
            0.40
            + spread_bps * 0.003
            + volatility * 0.12
            + (1.0 - liquidity) * 0.10
        )

        return [
            Hypothesis(
                name="hypothesis_buy", action="BUY",
                score=buy_score, confidence=buy_score,
                reasons=[
                    f"trend_score={trend}",
                    f"price_change_pct={price_change}",
                    f"rsi={rsi}",
                    f"liquidity_score={liquidity}",
                ],
            ),
            Hypothesis(
                name="hypothesis_sell", action="SELL",
                score=sell_score, confidence=sell_score,
                reasons=[
                    f"trend_score={trend}",
                    f"price_change_pct={price_change}",
                    f"rsi={rsi}",
                    f"volume_change_pct={volume_change}",
                ],
            ),
            Hypothesis(
                name="hypothesis_hold", action="HOLD",
                score=hold_score, confidence=hold_score,
                reasons=[
                    f"volatility={volatility}",
                    f"spread_bps={spread_bps}",
                    f"liquidity_score={liquidity}",
                ],
            ),
            Hypothesis(
                name="hypothesis_observe", action="OBSERVE",
                score=observe_score, confidence=observe_score,
                reasons=[
                    "Observation preferred when market quality is weak.",
                    f"spread_bps={spread_bps}",
                    f"volatility={volatility}",
                ],
            ),
        ]

    def _size_from_confidence(self, confidence: float) -> float:
        if confidence < self.min_commitment:
            return 0.0
        raw = (confidence - self.min_commitment) / (1.0 - self.min_commitment)
        return self._clamp(raw) * self.max_shadow_size

    def _memory_tags(
        self, snapshot: Dict[str, Any], action: Action, confidence: float,
    ) -> List[str]:
        tags = [f"action:{action}", f"confidence:{round(confidence, 2)}"]
        if float(snapshot.get("spread_bps", 9999.0)) > 25:
            tags.append("wide_spread")
        if float(snapshot.get("volatility", 0.0)) > 0.7:
            tags.append("high_volatility")
        if abs(float(snapshot.get("trend_score", 0.0))) > 0.6:
            tags.append("strong_trend")
        return tags

    @staticmethod
    def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
        if math.isnan(x) or math.isinf(x):
            return lo
        return max(lo, min(hi, x))
