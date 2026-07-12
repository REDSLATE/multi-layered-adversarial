"""Shared base for the four MC Pulse brains.

2026-07-12 doctrine (P7a): every brain now supplies its own
`Strategy` implementation. The `Strategy.evaluate(snapshot)`
returns a raw `StrategyResult`; the base class:

  1. Runs the strategy (sub-millisecond, deterministic).
  2. Applies the personality confidence multiplier.
  3. Wraps into a `ModelOpinion` with the standard rank inputs.
  4. Persists a manifest hint for parity/audit.

The old `NeutralAdversarialBrain` wrapper in `_legacy/` is now
UNUSED by production code — kept only until P7d deletes it.

Subclasses set 6 class-level identity constants and one class-
level `STRATEGY_CLS`. That's it. Everything else — the
should_evaluate cool-down, the required-field gate, the manifest
hint bookkeeping, the personality clamp, the rank input
mapping — is inherited so a Barracuda-vs-Hellcat divergence can
NEVER accidentally arise from orchestration drift.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import ClassVar, Optional, Type

from mc_brains.personality import apply_personality_confidence
from mc_brains.strategies import Strategy, StrategyResult
from mc_arbiter.models import Direction, ModelOpinion, OpinionStatus
from mc_pulse.snapshot import MarketSnapshot

logger = logging.getLogger("mc_brains._pulse_base")


@dataclass
class PulseManifestHint:
    """Bundle the pulse loop uses to persist a runner-comparable
    input manifest. Populated inside `evaluate()` at the moment the
    opinion is finalized so the manifest's action/confidence/status
    match exactly what got emitted.
    """
    feature_snapshot: dict = field(default_factory=dict)
    action: str = "HOLD"
    confidence: float = 0.0
    status: str = OpinionStatus.OK.value
    reason_codes: tuple[str, ...] = ()
    fallback_used: bool = False
    bar_count: int = 0
    position_context_present: bool = False


class NeutralAdversarialPulseBrain:
    """Base class for a pulse brain that dispatches to a strategy.

    Subclasses set 7 class-level constants. Everything else is
    inherited unchanged so Barracuda ≠ Hellcat divergence can
    only come from the strategy or personality, never from the
    orchestration layer.
    """

    # ── subclass MUST override ──
    PULSE_ID: ClassVar[str] = ""
    CORE_BRAIN_ID: ClassVar[str] = ""
    DISPLAY_NAME: ClassVar[str] = ""
    RATIONALE_TAG: ClassVar[str] = ""
    STRATEGY_CLS: ClassVar[Type[Strategy] | None] = None

    # ── subclass MAY override ──
    LANES: ClassVar[frozenset[str]] = frozenset({"equity", "crypto"})
    CADENCE_SECONDS: ClassVar[int] = 30
    EVAL_TIMEOUT_SECS: ClassVar[float] = 2.0

    def __init__(self) -> None:
        if not (self.PULSE_ID and self.CORE_BRAIN_ID and self.DISPLAY_NAME):
            raise TypeError(
                f"{type(self).__name__} MUST set PULSE_ID + CORE_BRAIN_ID + "
                "DISPLAY_NAME as class-level constants"
            )
        if self.STRATEGY_CLS is None:
            raise TypeError(
                f"{type(self).__name__} MUST set STRATEGY_CLS (2026-07-12 "
                "P7a doctrine — the shared NeutralAdversarialBrain core "
                "is retired; every brain owns its own strategy)"
            )
        self._strategy: Strategy = self.STRATEGY_CLS()  # type: ignore[assignment]
        self._last_eval_at: dict[str, datetime] = {}
        self._last_hint: dict[str, PulseManifestHint] = {}

    # ── Brain protocol properties ──
    @property
    def id(self) -> str:
        return self.PULSE_ID

    @property
    def lanes(self) -> frozenset[str]:
        return self.LANES

    @property
    def cadence_seconds(self) -> int:
        return self.CADENCE_SECONDS

    @property
    def evaluation_timeout_seconds(self) -> float:
        return self.EVAL_TIMEOUT_SECS

    def should_evaluate(
        self, *, now: datetime, snapshot: MarketSnapshot,
    ) -> bool:
        key = f"{snapshot.lane}:{snapshot.symbol}"
        last = self._last_eval_at.get(key)
        if last is not None and (now - last).total_seconds() < self.CADENCE_SECONDS:
            return False
        self._last_eval_at[key] = now
        return True

    def take_manifest_hint(self, symbol: str) -> Optional[PulseManifestHint]:
        return self._last_hint.pop(symbol.upper(), None)

    async def evaluate(
        self, snapshot: MarketSnapshot,
    ) -> Optional[ModelOpinion]:
        """Adapt pulse snapshot → Strategy.evaluate → ModelOpinion.

        Emits `INSUFFICIENT_DATA` (with confidence 0.0) when the
        strategy returns HOLD with confidence 0.0 (the family's
        NO_SIGNAL branch). All other outputs — including HOLD with
        non-zero confidence — are emitted as OK opinions so parity
        + distinctness math sees every genuine call the brain made.
        """
        canonical = dict(snapshot.feature_snapshot) or {}
        if not canonical:
            canonical = {
                "symbol": snapshot.symbol,
                "price": float(snapshot.price),
                "market_regime": snapshot.market_state,
                **dict(snapshot.indicators),
            }
        canonical.setdefault("symbol", snapshot.symbol)
        canonical.setdefault("price", float(snapshot.price))
        canonical.setdefault("market_regime", snapshot.market_state)

        position_context = snapshot.position_context.get(self.PULSE_ID) or None
        position_present = position_context is not None

        try:
            raw: StrategyResult = self._strategy.evaluate(snapshot)
        except Exception:
            logger.exception(
                "%s: strategy raised for %s (%s)",
                type(self).__name__, snapshot.symbol, snapshot.lane,
            )
            raise

        # NO_SIGNAL branches emit INSUFFICIENT_DATA.
        if raw.confidence == 0.0 and any(
            "NO_SIGNAL" in c for c in raw.reason_codes
        ):
            self._last_hint[snapshot.symbol.upper()] = PulseManifestHint(
                feature_snapshot=canonical,
                action=raw.action, confidence=0.0,
                status=OpinionStatus.INSUFFICIENT_DATA.value,
                reason_codes=raw.reason_codes,
                fallback_used=bool(snapshot.fallback_used),
                bar_count=int(snapshot.source_bar_count or 0),
                position_context_present=position_present,
            )
            return ModelOpinion(
                brain=self.PULSE_ID, seat_key="",
                direction=Direction.FLAT,
                edge=0.0, confidence=0.0,
                regime_fit=0.4, urgency=0.0,
                price_at_signal=float(snapshot.price),
                ts=snapshot.timestamp.isoformat(),
                rationale=(
                    f"{self.PULSE_ID}/insufficient_data · "
                    f"{','.join(raw.reason_codes[:4])}"
                ),
                status=OpinionStatus.INSUFFICIENT_DATA.value,
                reason_codes=raw.reason_codes,
            )

        # Apply personality clamp — RAW → FINAL confidence.
        final_confidence, persona_evidence = apply_personality_confidence(
            brain=self.CORE_BRAIN_ID,
            raw_confidence=raw.confidence,
        )

        direction = _map_action_to_direction(raw.action)
        if direction is None:
            return None

        rank_inputs = _rank_inputs_from_strategy(raw)

        # Rationale carries the STRATEGY reasoning path — this is
        # what makes distinctness observable in the audit tape.
        top_codes = ",".join(raw.reason_codes[:3]) or "none"
        rationale = (
            f"{self.PULSE_ID}/{self.RATIONALE_TAG} · "
            f"{raw.action} · raw_conf={raw.confidence:.2f} · "
            f"persona_x={persona_evidence['personality_multiplier']:.2f} · "
            f"reasons=[{top_codes}]"
        )

        opinion = ModelOpinion(
            brain=self.PULSE_ID, seat_key="",
            direction=direction,
            edge=rank_inputs["edge"],
            confidence=final_confidence,
            regime_fit=rank_inputs["regime_fit"],
            urgency=rank_inputs["urgency"],
            price_at_signal=float(snapshot.price),
            ts=snapshot.timestamp.isoformat(),
            rationale=rationale,
            status=OpinionStatus.OK.value,
            reason_codes=raw.reason_codes,
        )
        self._last_hint[snapshot.symbol.upper()] = PulseManifestHint(
            feature_snapshot=canonical,
            action=raw.action, confidence=final_confidence,
            status=OpinionStatus.OK.value,
            reason_codes=raw.reason_codes,
            fallback_used=bool(snapshot.fallback_used),
            bar_count=int(snapshot.source_bar_count or 0),
            position_context_present=position_present,
        )
        return opinion


def _map_action_to_direction(action: str) -> Optional[Direction]:
    a = (action or "").upper()
    if a == "BUY":
        return Direction.LONG
    if a == "SELL":
        return Direction.SHORT
    if a == "HOLD":
        return Direction.FLAT
    return None


def _rank_inputs_from_strategy(raw: StrategyResult) -> dict:
    """Map StrategyResult → DAWE rank inputs. Edge tracks the raw
    strategy conviction. `regime_fit` and `urgency` stay in the
    conservative middle band until a downstream module explicitly
    consumes them."""
    edge = max(0.0, min(1.0, raw.confidence))
    regime_fit = 0.60
    urgency = 0.50
    return {"edge": edge, "regime_fit": regime_fit, "urgency": urgency}
