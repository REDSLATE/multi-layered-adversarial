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

Audit checklist: `/app/memory/CAMINO_RUNNER_AUDIT.md`.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

# ── Legacy strategy body (survives step 8; deletion is contract-only) ──
# The `NeutralAdversarialBrain` template is what Camino has been
# running today. Reusing it here means we're not rewriting strategy
# logic during the pulse migration — just changing WHO calls it.
# The runner's own tick loop / heartbeats / dedup guard code stops
# running once step 7 fires; the strategy body underneath survives
# as a private implementation detail of CaminoBrain.
from external.brains.brain_core import NeutralAdversarialBrain
from external.brains.personality import apply_personality_confidence

from mc_arbiter.models import Direction, ModelOpinion
from mc_pulse.snapshot import MarketSnapshot

logger = logging.getLogger("mc_brains.camino")


class CaminoBrain:
    """Trend-follower brain. Pilot for the runner-free architecture.

    Fields required by `mc_pulse.protocols.Brain`:
        id                             "camino"
        lanes                          {"equity", "crypto"}
        cadence_seconds                30 (every other 15s pulse)
        evaluation_timeout_seconds     2.0

    Instance state persists between pulses — this is where the
    brain's memory lives (rolling ATR baselines, drawdown state,
    doctrine hysteresis, whatever the strategy accumulates).
    """

    id = "camino"
    lanes = frozenset({"equity", "crypto"})
    cadence_seconds = 30
    evaluation_timeout_seconds = 2.0

    def __init__(self) -> None:
        # brain_id="alpha" is Camino's internal slot code — the
        # personality module + doctrine module both key off this.
        # Display-name "camino" is what the pulse layer sees.
        self._core = NeutralAdversarialBrain(
            brain_id="alpha",
            display_name="Camino",
            lane="equity",              # per-call override in evaluate()
            shadow_only=True,           # sizing done by MC arbiter, not brain
            doctrine=None,              # legacy doctrine bind is fine; brain_core still respects overrides
        )
        # Per-lane last-eval timestamp — supports `should_evaluate`
        # honoring the brain's own cadence.
        self._last_eval_at: dict[str, datetime] = {}

    def should_evaluate(
        self,
        *,
        now: datetime,
        snapshot: MarketSnapshot,
    ) -> bool:
        """Every other 15s pulse. Same 30s tick Camino ran under
        its runner — parity preserved.

        Keyed by (lane, symbol) so a fast crypto symbol doesn't
        block a slow equity symbol on the same brain.
        """
        key = f"{snapshot.lane}:{snapshot.symbol}"
        last = self._last_eval_at.get(key)
        if last is not None and (now - last).total_seconds() < self.cadence_seconds:
            return False
        self._last_eval_at[key] = now
        return True

    async def evaluate(
        self,
        snapshot: MarketSnapshot,
    ) -> Optional[ModelOpinion]:
        """Adapt the pulse snapshot → NeutralAdversarialBrain input,
        run the strategy, wrap the output as a ModelOpinion.

        Returns None if the underlying brain declines to speak
        (below-floor with no directional conviction is HOLD, not
        None — HOLD is graded).
        """
        # Build the legacy-shape snapshot dict the core expects.
        # We copy indicators through so trend/momentum features
        # Camino relies on (rvol, ema20, macd_hist) reach the
        # strategy body untouched.
        core_snapshot = {
            "symbol": snapshot.symbol,
            "lane": snapshot.lane,
            "price": float(snapshot.price),
            "timestamp": snapshot.timestamp.isoformat(),
            "market_regime": snapshot.market_state,
            "market_state": snapshot.market_state,
            **dict(snapshot.indicators),
        }

        # Position context is MC's responsibility to inject. We
        # read ONLY our own brain's slot from the snapshot's
        # per-brain map — no peeking at peers' positions
        # (peer info would create a channel that couples brains).
        position_context = snapshot.position_context.get(self.id) or None
        try:
            brain_intent = self._core.evaluate(
                symbol=snapshot.symbol,
                snapshot=core_snapshot,
                position_context=position_context,
                seat=None,
            )
        except Exception:
            logger.exception(
                "CaminoBrain: core evaluate raised for %s (%s)",
                snapshot.symbol, snapshot.lane,
            )
            # Containment layer in mc_pulse turns this into a
            # BrainFailure receipt, but re-raise so the pulse can
            # log the stack trace properly. `evaluate_brain`
            # catches Exception explicitly.
            raise

        # Personality multiplier + audit stamp (audit row #2).
        # brain_id="alpha" so the personality module resolves
        # correctly against BRAIN_PERSONALITIES.
        final_confidence, persona_evidence = apply_personality_confidence(
            brain="alpha",
            raw_confidence=brain_intent.confidence,
        )

        direction = _map_action_to_direction(brain_intent.action)
        if direction is None:
            # Only happens if the core returns an unexpected action
            # string — treat as "brain declined to speak."
            return None

        # Camino's rank inputs. edge / regime_fit / urgency
        # derived from the brain's own reasoning; not-perfect
        # mappings but honest given the migration constraint of
        # reusing the legacy core unchanged.
        rank_inputs = _rank_inputs_from_brain(brain_intent, snapshot)

        return ModelOpinion(
            brain="camino",
            seat_key="",                       # filled by MC in the envelope wrap
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
        )


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
    regime_fit inversely proportional to market_quality_score
    (poor quality → low fit), urgency default 0.50.

    Phase 2 will replace this with brain-native rank inputs once
    strategy body is refactored to a native `.evaluate` (i.e.,
    when the legacy `NeutralAdversarialBrain` core is finally
    retired). Until then the numbers still come from the brain,
    just via a translation layer.
    """
    scores = brain_intent.hypothesis_scores or {}
    top = max(scores.values(), default=float(brain_intent.confidence))
    edge = max(0.0, min(1.0, float(top)))
    quality = float(brain_intent.market_quality_score or 0.0)
    # Poor quality (score → 1.0) collapses regime_fit toward 0.40.
    # Normal quality (score → 0.0) keeps it at 1.00.
    regime_fit = max(0.40, 1.00 - 0.60 * quality)
    urgency = 0.50
    return {"edge": edge, "regime_fit": regime_fit, "urgency": urgency}
