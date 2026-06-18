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
    current_side: Optional[str] = None
    signed_qty: Optional[float] = None
    target_exposure: Optional[str] = None
    transition_intent: Optional[str] = None
    order_action: Optional[str] = None
    position_evolution: Optional[str] = None
    risk_transition: Optional[str] = None
    # ── Doctrine + seat layer (operator directive, 2026-06-XX) ──
    # `doctrine` is bound to brain_id (immutable — Camino thinks like
    # a trend follower regardless of what job she's doing today).
    # `seat` is the brain's current runtime job — strategist, executor,
    # governor, or auditor — operator-rotatable without touching
    # doctrine. The pair lets the dashboard show "Camino is auditor
    # today" while the brain still emits trend-doctrine intents.
    doctrine: Optional[str] = None    # trend | mean_reversion | breakout | momentum
    seat: Optional[str] = None        # strategist | executor | governor | auditor


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
        doctrine: Optional[Any] = None,
    ):
        self.brain_id = brain_id
        self.display_name = display_name
        self.lane = lane
        self.shadow_only = shadow_only
        # If a doctrine is provided, its min_confidence/min_gap
        # override the constructor defaults — the doctrine module is
        # the source of truth for per-brain thresholds. The legacy
        # `min_commitment` arg stays for backward-compat with the
        # callers that haven't been migrated yet.
        if doctrine is not None:
            self.min_commitment = float(doctrine.min_confidence)
            self.min_gap = float(doctrine.min_gap)
        else:
            self.min_commitment = min_commitment
            self.min_gap = min_gap
        self.max_shadow_size = max_shadow_size
        self.doctrine = doctrine  # may be None for legacy callers

    def evaluate(
        self,
        symbol: str,
        snapshot: Dict[str, Any],
        position_context: Optional[Dict[str, Any]] = None,
        seat: Optional[str] = None,
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
        # Portfolio-manager layer — refine the primitive into the
        # SCALE_IN / SCALE_OUT / PARTIAL_COVER / FULL_COVER vocabulary
        # using brain confidence + position context. Computed even
        # when ctx is None (defaults to FLAT → OPEN passes through).
        position_evolution, risk_transition = self._derive_evolution(
            transition_intent, current_side,
            float(final_confidence),
            float(abs(signed_qty_val)),
            snapshot.get("market_regime", ""),
        )
        if ctx:
            reasoning.append(
                f"trade_transition: order_action={order_action} "
                f"current_side={current_side} → "
                f"target_exposure={target_exposure} "
                f"transition_intent={transition_intent} "
                f"position_evolution={position_evolution} "
                f"risk_transition={risk_transition}"
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
            position_evolution=position_evolution,
            risk_transition=risk_transition,
            doctrine=(self.doctrine.doctrine if self.doctrine is not None else None),
            seat=seat,
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

    # ── Portfolio-manager-grade refinement (2026-06-XX) ─────────
    # Thresholds mirrored from shared.position_model so brain_core
    # stays a pure standalone module (the brain runner can ship
    # without importing MC's shared lib at decision time).
    SCALE_IN_CONF_FLOOR = 0.65
    SCALE_OUT_CONF_FLOOR = 0.55
    FULL_COVER_CONF_FLOOR = 0.78
    _RISK_OFF_REGIMES = ("volatile", "crisis", "stressed", "risk_off")
    _RISK_ON_REGIMES = ("calm", "bullish", "trend", "risk_on")

    @staticmethod
    def _derive_evolution(
        transition_intent: str,
        current_side: str,
        confidence: float,
        abs_position_qty: float,
        market_regime: str,
    ) -> tuple:
        """Lift the primitive transition into the portfolio-manager
        vocabulary and the portfolio-level risk verb.

        Returns:
            (position_evolution, risk_transition)

        Where:
            position_evolution ∈ {OPEN, ADD, REDUCE, CLOSE, FLIP,
                                  HOLD, SCALE_IN, SCALE_OUT,
                                  PARTIAL_COVER, FULL_COVER}
            risk_transition    ∈ {RISK_ON, RISK_OFF, NEUTRAL}

        Doctrine pin:
            SCALE_IN  is a planned ADD (conviction-driven).
            SCALE_OUT is a planned REDUCE on a LONG (lock-in gains).
            PARTIAL_COVER is a REDUCE on a SHORT (taking some off).
            FULL_COVER  is a CLOSE on a SHORT (full flat).

        Reasoning is mirrored in `shared.position_model.
        classify_position_evolution` so the audit log and the brain
        always agree on the verb.
        """
        side = (current_side or "FLAT").upper()
        base = (transition_intent or "HOLD").upper()
        c = float(confidence or 0.0)
        regime = (market_regime or "").lower().strip()

        # Primitives that pass through unchanged.
        if base in ("HOLD", "FLIP", "OPEN"):
            evo = base
        elif base == "ADD":
            evo = ("SCALE_IN" if (side == "LONG" and
                                  c >= NeutralAdversarialBrain.SCALE_IN_CONF_FLOOR)
                   else "ADD")
        elif base == "REDUCE":
            if side == "LONG":
                evo = ("SCALE_OUT" if c >= NeutralAdversarialBrain.SCALE_OUT_CONF_FLOOR
                       else "REDUCE")
            elif side == "SHORT":
                evo = "PARTIAL_COVER"
            else:
                evo = "REDUCE"
        elif base == "CLOSE":
            if side == "SHORT":
                evo = ("FULL_COVER" if c >= NeutralAdversarialBrain.FULL_COVER_CONF_FLOOR
                       else "PARTIAL_COVER")
            else:
                evo = "CLOSE"
        else:
            evo = base

        # Risk-transition lift.
        de_risk = {"REDUCE", "CLOSE", "SCALE_OUT", "PARTIAL_COVER", "FULL_COVER"}
        add_risk = {"OPEN", "ADD", "SCALE_IN"}
        if regime in NeutralAdversarialBrain._RISK_OFF_REGIMES and evo in de_risk:
            risk = "RISK_OFF"
        elif regime in NeutralAdversarialBrain._RISK_ON_REGIMES and evo in add_risk:
            risk = "RISK_ON"
        elif regime in NeutralAdversarialBrain._RISK_OFF_REGIMES and evo == "FLIP":
            risk = "RISK_OFF"
        else:
            risk = "NEUTRAL"

        return (evo, risk)

    def _build_hypotheses(self, s: Dict[str, Any]) -> List[Hypothesis]:
        price_change = float(s.get("price_change_pct", 0.0))
        volume_change = float(s.get("volume_change_pct", 0.0))
        rsi = float(s.get("rsi", 50.0))
        spread_bps = float(s.get("spread_bps", 9999.0))
        volatility = float(s.get("volatility", 0.0))
        trend = float(s.get("trend_score", 0.0))
        liquidity = float(s.get("liquidity_score", 0.5))
        setup_score = float(s.get("setup_score", 0.0))

        # ── Doctrine-driven scoring (operator directive, 2026-06-XX) ──
        # Each brain decomposes the snapshot into four named signal
        # components and weights them by its doctrine. The same
        # snapshot now yields four DIFFERENT hypothesis rankings
        # across the four brains — which is the point of having
        # four brains in the first place.
        #
        # Signal derivations (kept simple and explicit so the
        # interpretation can be audited):
        #   trend_signal      — from trend_score; signed
        #   mean_rev_signal   — from RSI extremes (overbought/oversold)
        #   breakout_signal   — from setup_score (BASE-BREAKOUT pattern)
        #                       blended with volume confirmation
        #   momentum_signal   — from price_change * sign(volume_change)
        #   risk_penalty      — from volatility + spread (illiquidity)
        if self.doctrine is not None:
            return self._build_hypotheses_doctrine(
                trend=trend, rsi=rsi, setup_score=setup_score,
                price_change=price_change, volume_change=volume_change,
                volatility=volatility, spread_bps=spread_bps,
                liquidity=liquidity,
            )

        # Legacy path — unchanged behavior for any caller still
        # constructing the brain without a doctrine.
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

    def _build_hypotheses_doctrine(
        self, *,
        trend: float, rsi: float, setup_score: float,
        price_change: float, volume_change: float,
        volatility: float, spread_bps: float, liquidity: float,
    ) -> List[Hypothesis]:
        """Doctrine-weighted hypothesis builder.

        Each brain interprets the SAME snapshot through its own
        doctrine weights, so Camino (trend) and Barracuda
        (mean_reversion) reach genuinely different conclusions on
        the same input — that's the disagreement the gate chain
        needs to do its job.
        """
        d = self.doctrine
        # Signal decomposition — same inputs, four named axes.
        trend_signal = trend                                    # [-1, +1]
        mean_rev_signal = (50.0 - rsi) / 50.0                   # [-1, +1]; positive = oversold (buy mean rev)
        breakout_signal = self._clamp(
            setup_score + max(0.0, volume_change / 200.0),       # blend pattern + volume confirm
            lo=0.0, hi=1.5,
        )
        momentum_signal = (price_change / 5.0) * (
            1.0 if volume_change >= 0 else 0.5
        )                                                        # ~[-1, +1] for ±5% moves
        risk_penalty = (volatility * 0.6) + (spread_bps * 0.003)

        # Doctrine-weighted composite for the BUY hypothesis. Each
        # weight scales the signal's contribution. Doctrines that
        # don't care about a signal don't get its volatility either.
        buy_composite = (
            trend_signal * d.trend_weight * 0.20
            + mean_rev_signal * d.mean_reversion_weight * 0.18
            + breakout_signal * d.breakout_weight * 0.20
            + momentum_signal * d.momentum_weight * 0.20
            - risk_penalty * d.risk_weight * 0.10
            + liquidity * 0.05
        )
        sell_composite = (
            -trend_signal * d.trend_weight * 0.20
            - mean_rev_signal * d.mean_reversion_weight * 0.18
            + breakout_signal * d.breakout_weight * 0.10  # breakout DOWN is still a setup
            - momentum_signal * d.momentum_weight * 0.20
            - risk_penalty * d.risk_weight * 0.10
            + liquidity * 0.05
        )
        hold_composite = (
            0.45
            + volatility * 0.20
            + spread_bps * 0.002
            + (1.0 - liquidity) * 0.15
            - abs(trend_signal) * d.trend_weight * 0.08
            - abs(momentum_signal) * d.momentum_weight * 0.05
        )
        observe_composite = (
            0.40
            + spread_bps * 0.003
            + volatility * 0.12
            + (1.0 - liquidity) * 0.10
        )

        # Aggression scales the brain's commitment toward action
        # hypotheses (BUY/SELL) without affecting HOLD/OBSERVE.
        # Aggression < 1.0 dampens; > 1.0 amplifies.
        agg = float(d.aggression)
        buy_score = self._clamp(0.50 + buy_composite * agg)
        sell_score = self._clamp(0.50 + sell_composite * agg)
        hold_score = self._clamp(hold_composite)
        observe_score = self._clamp(observe_composite)

        reasons_signal = [
            f"doctrine={d.doctrine} agg={d.aggression:.2f}",
            f"trend_signal={trend_signal:.3f} (w={d.trend_weight:.2f})",
            f"mean_rev_signal={mean_rev_signal:.3f} (w={d.mean_reversion_weight:.2f})",
            f"breakout_signal={breakout_signal:.3f} (w={d.breakout_weight:.2f})",
            f"momentum_signal={momentum_signal:.3f} (w={d.momentum_weight:.2f})",
            f"risk_penalty={risk_penalty:.3f} (w={d.risk_weight:.2f})",
        ]
        return [
            Hypothesis(
                name="hypothesis_buy", action="BUY",
                score=buy_score, confidence=buy_score,
                reasons=reasons_signal + [f"buy_composite={buy_composite:.3f}"],
            ),
            Hypothesis(
                name="hypothesis_sell", action="SELL",
                score=sell_score, confidence=sell_score,
                reasons=reasons_signal + [f"sell_composite={sell_composite:.3f}"],
            ),
            Hypothesis(
                name="hypothesis_hold", action="HOLD",
                score=hold_score, confidence=hold_score,
                reasons=[
                    f"volatility={volatility:.3f}",
                    f"spread_bps={spread_bps:.1f}",
                    f"liquidity={liquidity:.2f}",
                ],
            ),
            Hypothesis(
                name="hypothesis_observe", action="OBSERVE",
                score=observe_score, confidence=observe_score,
                reasons=[
                    "Market quality is weak — observation preferred.",
                    f"spread_bps={spread_bps:.1f}",
                    f"volatility={volatility:.3f}",
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
