"""CaminoBrain — pilot for the MC pulse migration.

Doctrine: **trend follower**. Balanced personality (×1.00
confidence multiplier). Runs both lanes but leans equity.

Wraps the existing `NeutralAdversarialBrain` template (already in
`/app/external/brains/brain_core.py`) so the strategy logic +
memory + thresholds are reused verbatim during the migration
window. Only the OUTER SHAPE changes — from a runner-driven
`.evaluate(symbol, snapshot, position_context, seat)` returning
a `BrainIntent`, to the pulse-driven `.evaluate(snapshot)`
returning a `ModelOpinion`.

2026-02 parity work — three contract changes vs the initial
pulse Camino:

    1. Consumes `snapshot.feature_snapshot` (the canonical
       Camino feature dict) instead of `snapshot.indicators`.
       This eliminates the impoverished-input path that pinned
       `hold_score` to 1.0 via `spread_bps=9999` defaults.

    2. If ANY field in CAMINO_REQUIRED_FIELDS is missing, the
       brain does NOT invoke the legacy core. It emits an
       explicit `status=INSUFFICIENT_DATA, confidence=0.0` opinion
       so parity math can strip these out of "action match" and
       downstream arbitration can distinguish "no signal" from
       "confident HOLD".

    3. Returns `(opinion, manifest_hint)` — the pulse loop uses
       `manifest_hint` to record the runner-comparable input
       manifest under the canonical ParityKey.

Audit checklist: `/app/memory/CAMINO_RUNNER_AUDIT.md`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from external.brains.brain_core import NeutralAdversarialBrain
from external.brains.personality import apply_personality_confidence

from mc_arbiter.models import Direction, ModelOpinion, OpinionStatus
from mc_pulse.input_manifest import CAMINO_REQUIRED_FIELDS
from mc_pulse.snapshot import MarketSnapshot

logger = logging.getLogger("mc_brains.camino")


@dataclass
class CaminoManifestHint:
    """Bundle the pulse loop uses to persist a runner-comparable
    input manifest. Populated inside `CaminoBrain.evaluate` at
    the same moment the opinion is finalized so the manifest's
    action/confidence/status match exactly what got emitted."""
    feature_snapshot: dict = field(default_factory=dict)
    action: str = "HOLD"
    confidence: float = 0.0
    status: str = OpinionStatus.OK.value
    reason_codes: tuple[str, ...] = ()
    fallback_used: bool = False
    bar_count: int = 0
    position_context_present: bool = False


class CaminoBrain:
    """Trend-follower brain. Pilot for the runner-free architecture.

    Fields required by `mc_pulse.protocols.Brain`:
        id                             "camino"
        lanes                          {"equity", "crypto"}
        cadence_seconds                30 (every other 15s pulse)
        evaluation_timeout_seconds     2.0
    """

    id = "camino"
    lanes = frozenset({"equity", "crypto"})
    cadence_seconds = 30
    evaluation_timeout_seconds = 2.0

    def __init__(self) -> None:
        self._core = NeutralAdversarialBrain(
            brain_id="alpha",
            display_name="Camino",
            lane="equity",              # per-call override in evaluate()
            shadow_only=True,
            doctrine=None,
        )
        self._last_eval_at: dict[str, datetime] = {}
        # `manifest_hint` bookkeeping — the pulse loop reads this
        # AFTER `evaluate` returns to persist a manifest under the
        # correct ParityKey. Kept per-instance (not per-call) so
        # concurrent evaluations across symbols don't collide.
        self._last_hint: dict[str, CaminoManifestHint] = {}

    def should_evaluate(
        self,
        *,
        now: datetime,
        snapshot: MarketSnapshot,
    ) -> bool:
        key = f"{snapshot.lane}:{snapshot.symbol}"
        last = self._last_eval_at.get(key)
        if last is not None and (now - last).total_seconds() < self.cadence_seconds:
            return False
        self._last_eval_at[key] = now
        return True

    def take_manifest_hint(self, symbol: str) -> Optional[CaminoManifestHint]:
        """Pulse loop drains the hint after `evaluate` so it can
        persist a manifest under the canonical ParityKey. `pop`
        semantics — a hint is used exactly once."""
        return self._last_hint.pop(symbol.upper(), None)

    async def evaluate(
        self,
        snapshot: MarketSnapshot,
    ) -> Optional[ModelOpinion]:
        """Adapt the pulse snapshot → NeutralAdversarialBrain input,
        run the strategy, wrap the output as a ModelOpinion.

        Returning `INSUFFICIENT_DATA` with confidence=0.0 is
        different from returning None. None means "brain didn't
        want a shot at this snapshot" (should_evaluate=False).
        INSUFFICIENT_DATA means "brain did want a shot but the
        inputs required by its core were absent" — the pulse
        loop STILL persists this envelope so parity math sees
        every gated event.
        """
        # Canonical feature dict from the pulse SnapshotService.
        # If the dict is empty (legacy caller / test with a bare
        # snapshot), fall back to `indicators` so tests written
        # before this contract don't break.
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

        position_context = snapshot.position_context.get(self.id) or None
        position_present = position_context is not None

        # Required-field gate — the primary fix for the HOLD @ 1.0
        # signature. If ANY required field is absent, we do NOT
        # invoke the core with defaults; we emit an explicit
        # INSUFFICIENT_DATA opinion so downstream parity math and
        # arbitration can distinguish this from a confident HOLD.
        missing = sorted(f for f in CAMINO_REQUIRED_FIELDS if f not in canonical or canonical.get(f) is None)
        if missing:
            self._last_hint[snapshot.symbol.upper()] = CaminoManifestHint(
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
                brain="camino",
                seat_key="",
                direction=Direction.FLAT,
                edge=0.0,
                confidence=0.0,
                regime_fit=0.4,          # neutral lane multiplier floor
                urgency=0.0,
                price_at_signal=float(snapshot.price),
                ts=snapshot.timestamp.isoformat(),
                rationale=(
                    f"camino/insufficient_data · missing="
                    f"{','.join(missing[:8])}"
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
                "CaminoBrain: core evaluate raised for %s (%s)",
                snapshot.symbol, snapshot.lane,
            )
            raise

        final_confidence, persona_evidence = apply_personality_confidence(
            brain="alpha",
            raw_confidence=brain_intent.confidence,
        )

        direction = _map_action_to_direction(brain_intent.action)
        if direction is None:
            return None

        rank_inputs = _rank_inputs_from_brain(brain_intent, snapshot)

        opinion = ModelOpinion(
            brain="camino",
            seat_key="",
            direction=direction,
            edge=rank_inputs["edge"],
            confidence=final_confidence,
            regime_fit=rank_inputs["regime_fit"],
            urgency=rank_inputs["urgency"],
            price_at_signal=float(snapshot.price),
            ts=snapshot.timestamp.isoformat(),
            rationale=(
                f"camino/trend · quality={brain_intent.market_quality_score:.2f}"
                f" · {brain_intent.action}"
                f" · persona_x={persona_evidence['personality_multiplier']:.2f}"
                f" · saturated={persona_evidence['saturated_by_clamp']}"
            ),
            status=OpinionStatus.OK.value,
        )
        self._last_hint[snapshot.symbol.upper()] = CaminoManifestHint(
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
    """BUY / SELL / HOLD / OBSERVE → LONG / SHORT / FLAT / None.

    OBSERVE is a market-quality modifier (not a direction) per
    the 2026-02-21 doctrine — the core surfaces it as
    `market_quality_score` on the intent, so an OBSERVE landing
    here means the core is misbehaving. Return None to skip.
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
    """Map the legacy BrainIntent onto DAWE's rank inputs.

    v0.1: honest-but-crude — edge = winning hypothesis score,
    regime_fit inversely proportional to market_quality_score,
    urgency default 0.50.
    """
    scores = brain_intent.hypothesis_scores or {}
    top = max(scores.values(), default=float(brain_intent.confidence))
    edge = max(0.0, min(1.0, float(top)))
    quality = float(brain_intent.market_quality_score or 0.0)
    regime_fit = max(0.40, 1.00 - 0.60 * quality)
    urgency = 0.50
    return {"edge": edge, "regime_fit": regime_fit, "urgency": urgency}
