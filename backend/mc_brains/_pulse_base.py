"""Shared base for the four MC Pulse brains.

All 4 brains (Camino / Barracuda / Hellcat / GTO) wrap the same
legacy `NeutralAdversarialBrain` core — personality is a
CONFIDENCE MULTIPLIER, not a distinct strategy. Extracted from
the original one-brain-per-file pattern on 2026-07-12 during the
P2 migration (three new pulse brains ship at once, all sharing
this base).

Subclasses override class-level identity constants:

    PULSE_ID          — string id in the pulse registry ("camino",
                        "gto", "barracuda", "hellcat"). Used as
                        `ModelOpinion.brain` and as the operator-
                        facing brand name across every UI + audit
                        row. Matches `personality.get_personality`
                        lookup by inverse mapping (display_name).
    CORE_BRAIN_ID     — internal DB / API slot code ("alpha",
                        "redeye", "camaro", "chevelle"). This is
                        what `apply_personality_confidence` keys
                        on — the personality multiplier is a
                        property of the SLOT, not the brand.
    DISPLAY_NAME      — human-readable brand (matches
                        `personality.BRAIN_PERSONALITIES`).
    RATIONALE_TAG     — leaf token that identifies the brain's
                        voice in rationale strings ("trend",
                        "opportunistic", "aggressive",
                        "disciplined"). No behavioral impact —
                        pure log signal.
    LANES             — frozenset of lanes the brain evaluates.
                        v0.1 all 4 brains cover {"equity", "crypto"}.
    CADENCE_SECONDS   — cool-down between evaluations of the same
                        (lane, symbol). Prevents burning identical
                        opinions on identical pulses. 30s = every
                        other 15s pulse tick.
    EVAL_TIMEOUT_SECS — per-evaluation timeout enforced by
                        `mc_pulse.containment.evaluate_brain`.
                        A brain that stalls past this is caught by
                        containment and does NOT block peers.

The `evaluate()` shape is IDENTICAL across all 4 subclasses:
consume the canonical feature snapshot, gate on required fields,
call the core, wrap the output as a `ModelOpinion`, stamp a
manifest hint. Only the identity constants differ.

Doctrine — this file MUST NOT accidentally reintroduce brain-
side gates. Every restriction lives in MC (broker toggles,
sizing gate, exposure caps, learning ladder). Personality here
is a confidence multiplier ONLY.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import ClassVar, Optional

from mc_brains._legacy.brain_core import NeutralAdversarialBrain
from mc_brains._legacy.personality import apply_personality_confidence

from mc_arbiter.models import Direction, ModelOpinion, OpinionStatus
from mc_pulse.input_manifest import CAMINO_REQUIRED_FIELDS
from mc_pulse.snapshot import MarketSnapshot

logger = logging.getLogger("mc_brains._pulse_base")

# All 4 pulse brains share the same input contract — they all use
# `NeutralAdversarialBrain._build_hypotheses` which reads the same
# feature set. The `CAMINO_` prefix predates the multi-brain roll-
# out; kept as-is for BC and re-aliased here for clarity.
NEUTRAL_ADVERSARIAL_REQUIRED_FIELDS = CAMINO_REQUIRED_FIELDS


@dataclass
class PulseManifestHint:
    """Bundle the pulse loop uses to persist a runner-comparable
    input manifest. Populated inside `evaluate()` at the moment the
    opinion is finalized so the manifest's action/confidence/status
    match exactly what got emitted.

    Renamed from `CaminoManifestHint` on 2026-07-12 during the
    multi-brain migration. `CaminoManifestHint` remains an alias
    in `mc_brains.camino` for backward compat with the pulse loop.
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
    """Base class for a pulse brain that wraps `NeutralAdversarialBrain`.

    Subclasses set 6 class-level constants. Everything else — the
    should_evaluate cool-down, the required-field gate, the manifest
    hint bookkeeping, the personality clamp, the rank input mapping —
    is inherited unchanged so a Barracuda vs. Hellcat divergence
    can NEVER accidentally arise from an inconsistency in the
    orchestration layer. All differences flow through personality
    (a confidence multiplier) and lane/cadence config.
    """

    # ── subclass MUST override ──
    PULSE_ID: ClassVar[str] = ""
    CORE_BRAIN_ID: ClassVar[str] = ""
    DISPLAY_NAME: ClassVar[str] = ""
    RATIONALE_TAG: ClassVar[str] = ""

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
        self._core = NeutralAdversarialBrain(
            brain_id=self.CORE_BRAIN_ID,
            display_name=self.DISPLAY_NAME,
            lane="equity",              # per-call override in evaluate()
            shadow_only=True,
            doctrine=None,
        )
        self._last_eval_at: dict[str, datetime] = {}
        self._last_hint: dict[str, PulseManifestHint] = {}

    # ── Brain protocol properties (populated from ClassVars) ──
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
        """Pop the last manifest hint for this symbol. Pulse loop
        calls this AFTER `evaluate` to persist a manifest row."""
        return self._last_hint.pop(symbol.upper(), None)

    async def evaluate(
        self, snapshot: MarketSnapshot,
    ) -> Optional[ModelOpinion]:
        """Adapt pulse snapshot → NeutralAdversarialBrain input, run
        the strategy, wrap the output as a ModelOpinion.

        Returning `INSUFFICIENT_DATA` w/ confidence=0.0 is different
        from returning None. None = brain skipped (cool-down).
        INSUFFICIENT_DATA = brain wanted to evaluate but required
        inputs were absent — pulse loop STILL persists this envelope
        so parity math sees every gated event.
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

        missing = sorted(
            f for f in NEUTRAL_ADVERSARIAL_REQUIRED_FIELDS
            if f not in canonical or canonical.get(f) is None
        )
        if missing:
            self._last_hint[snapshot.symbol.upper()] = PulseManifestHint(
                feature_snapshot=canonical,
                action="HOLD",
                confidence=0.0,
                status=OpinionStatus.INSUFFICIENT_DATA.value,
                reason_codes=("MISSING_REQUIRED_FEATURES", *missing[:6]),
                fallback_used=bool(snapshot.fallback_used),
                bar_count=int(snapshot.source_bar_count or 0),
                position_context_present=position_present,
            )
            return ModelOpinion(
                brain=self.PULSE_ID,
                seat_key="",
                direction=Direction.FLAT,
                edge=0.0,
                confidence=0.0,
                regime_fit=0.4,
                urgency=0.0,
                price_at_signal=float(snapshot.price),
                ts=snapshot.timestamp.isoformat(),
                rationale=(
                    f"{self.PULSE_ID}/insufficient_data · "
                    f"missing={','.join(missing[:8])}"
                ),
                status=OpinionStatus.INSUFFICIENT_DATA.value,
                reason_codes=("MISSING_REQUIRED_FEATURES", *missing[:6]),
            )

        try:
            brain_intent = self._core.evaluate(
                symbol=snapshot.symbol,
                snapshot=canonical,
                position_context=position_context,
                seat=None,
            )
        except Exception:
            logger.exception(
                "%s: core evaluate raised for %s (%s)",
                type(self).__name__, snapshot.symbol, snapshot.lane,
            )
            raise

        final_confidence, persona_evidence = apply_personality_confidence(
            brain=self.CORE_BRAIN_ID,
            raw_confidence=brain_intent.confidence,
        )

        direction = _map_action_to_direction(brain_intent.action)
        if direction is None:
            return None

        rank_inputs = _rank_inputs_from_brain(brain_intent, snapshot)

        opinion = ModelOpinion(
            brain=self.PULSE_ID,
            seat_key="",
            direction=direction,
            edge=rank_inputs["edge"],
            confidence=final_confidence,
            regime_fit=rank_inputs["regime_fit"],
            urgency=rank_inputs["urgency"],
            price_at_signal=float(snapshot.price),
            ts=snapshot.timestamp.isoformat(),
            rationale=(
                f"{self.PULSE_ID}/{self.RATIONALE_TAG} · "
                f"quality={brain_intent.market_quality_score:.2f} · "
                f"{brain_intent.action} · "
                f"persona_x={persona_evidence['personality_multiplier']:.2f} · "
                f"saturated={persona_evidence['saturated_by_clamp']}"
            ),
            status=OpinionStatus.OK.value,
        )
        self._last_hint[snapshot.symbol.upper()] = PulseManifestHint(
            feature_snapshot=canonical,
            action=brain_intent.action,
            confidence=final_confidence,
            status=OpinionStatus.OK.value,
            reason_codes=(),
            fallback_used=bool(snapshot.fallback_used),
            bar_count=int(snapshot.source_bar_count or 0),
            position_context_present=position_present,
        )
        return opinion


def _map_action_to_direction(action: str) -> Optional[Direction]:
    """BUY / SELL / HOLD → LONG / SHORT / FLAT.
    Anything else (OBSERVE — a quality modifier surfaced as
    `market_quality_score`) returns None → pulse skips the write.
    """
    a = (action or "").upper()
    if a == "BUY":
        return Direction.LONG
    if a == "SELL":
        return Direction.SHORT
    if a == "HOLD":
        return Direction.FLAT
    return None


def _rank_inputs_from_brain(brain_intent, snapshot: MarketSnapshot) -> dict:
    """Map legacy BrainIntent → DAWE rank inputs. v0.1: edge = top
    hypothesis score, regime_fit inversely proportional to
    market_quality_score, urgency default 0.50. Identical to what
    Camino did — the DAWE input math is a property of the core, not
    the personality.
    """
    scores = brain_intent.hypothesis_scores or {}
    top = max(scores.values(), default=float(brain_intent.confidence))
    edge = max(0.0, min(1.0, float(top)))
    quality = float(brain_intent.market_quality_score or 0.0)
    regime_fit = max(0.40, 1.00 - 0.60 * quality)
    urgency = 0.50
    return {"edge": edge, "regime_fit": regime_fit, "urgency": urgency}
