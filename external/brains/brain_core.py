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
distinct `brain_id` (one of MC's 4 internal slot codes: alpha,
camaro, chevelle, redeye) and a distinct `display_name`
(Camino / Barracuda / Hellcat / GTO — the operator-facing brand
shown on every dashboard, intent card, and audit row).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
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
    # ── Trade-transition layer (operator directive, 2026-06-XX) ──
    # Side awareness — derived from the live broker position_context
    # that the runner injects before evaluate(). These fields make
    # the difference between "BUY = open long" and "BUY against an
    # existing SHORT = cover" visible to MC and to the audit log.
    # None when the brain ran without a position_context (legacy).
    current_side: Optional[str] = None            # LONG | SHORT | FLAT
    signed_qty: Optional[float] = None
    target_exposure: Optional[str] = None         # LONG | SHORT | FLAT
    transition_intent: Optional[str] = None       # OPEN | ADD | REDUCE | CLOSE | FLIP | HOLD
    order_action: Optional[str] = None            # BUY | SELL (broker instruction)


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

    def evaluate(
        self,
        symbol: str,
        snapshot: Dict[str, Any],
        position_context: Optional[Dict[str, Any]] = None,
    ) -> BrainIntent:
        # ── Trade-transition awareness (operator directive, 2026-06-XX) ──
        # The runner injects a `position_context` describing the live
        # broker position the brain is acting against. Surface it on
        # the snapshot so reasoning carries the inventory state, and
        # let the hypothesis builder use it as descriptive evidence.
        ctx = position_context or snapshot.get("position_context")
        if ctx:
            snapshot = {**snapshot, "position_context": ctx}

        hypotheses = self._build_hypotheses(snapshot)
        winner, runner_up = sorted(
            hypotheses, key=lambda h: h.score, reverse=True,
        )[:2]
        gap = winner.score - runner_up.score

        reasoning: List[str] = [
            f"display_name={self.display_name} brain_id={self.brain_id} lane={self.lane}",
        ]
        if ctx:
            reasoning.append(
                f"position_context: current_side={ctx.get('current_side')} "
                f"signed_qty={ctx.get('signed_qty')} "
                f"allowed_transitions={ctx.get('allowed_transitions')}"
            )
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

        # Derive the transition layer fields. When we have no
        # position_context, default to FLAT — same semantics as
        # legacy BUY/SELL-only thinking, but explicit.
        current_side = (ctx or {}).get("current_side", "FLAT")
        signed_qty_val = float((ctx or {}).get("signed_qty", 0.0) or 0.0)
        transition_intent, target_exposure, order_action = self._derive_transition(
            final_action, current_side, signed_qty_val,
        )
        if ctx:
            reasoning.append(
                f"trade_transition: order_action={order_action} "
                f"current_side={current_side} → "
                f"target_exposure={target_exposure} "
                f"transition_intent={transition_intent}"
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
            current_side=current_side,
            signed_qty=signed_qty_val,
            target_exposure=target_exposure,
            transition_intent=transition_intent,
            order_action=order_action,
        )

    @staticmethod
    def _derive_transition(
        final_action: str, current_side: str, signed_qty: float,
    ) -> tuple:
        """Map (final_action, current_side) → (transition_intent,
        target_exposure, order_action).

        This is the operator-pinned vocabulary the brain MUST emit
        alongside the legacy `action` field. The richer 10-state
        classifier lives in `shared.position_model.classify_trade_transition`
        — we mirror its meaning here without importing it (brain_core
        stays a pure module so the runner can ship it standalone).

        Returns:
            (transition_intent, target_exposure, order_action)

        Where:
            transition_intent ∈ {OPEN, ADD, REDUCE, CLOSE, FLIP, HOLD}
            target_exposure   ∈ {LONG, SHORT, FLAT}
            order_action      ∈ {BUY, SELL, HOLD}
        """
        side = (current_side or "FLAT").upper()
        act = (final_action or "HOLD").upper()

        if act not in ("BUY", "SELL"):
            return ("HOLD", side, "HOLD")

        if act == "BUY":
            if side == "FLAT":
                return ("OPEN", "LONG", "BUY")
            if side == "LONG":
                return ("ADD", "LONG", "BUY")
            # side == SHORT — BUY against a short is a cover.
            # The brain emits CLOSE intent semantically; whether the
            # actual order_qty equals the full short magnitude is a
            # sizing decision the gate chain owns, not the brain.
            return ("CLOSE", "FLAT", "BUY")
        # act == SELL
        if side == "FLAT":
            return ("OPEN", "SHORT", "SELL")
        if side == "SHORT":
            return ("ADD", "SHORT", "SELL")
        # side == LONG — SELL against a long is a close.
        return ("CLOSE", "FLAT", "SELL")

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
