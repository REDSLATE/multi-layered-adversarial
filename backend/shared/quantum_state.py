"""RISEDUAL — Quantum-Inspired State Layer.

Quantum-inspired ONLY — no quantum hardware. This is a probability-field
regime detector + HOLD-lock signal + bounded risk modulator. Slots
between the council verdict and the final order size.

DOCTRINE (do not violate):
  * Does NOT choose BUY/SELL/SHORT
  * Does NOT promote HOLD into a trade
  * Does NOT reverse direction
  * MAY reduce or modestly modulate risk within [min_risk, max_risk]
  * MAY signal HOLD-lock when the council collapses into passive neutrality

Wiring (see execution.py _evaluate_council):
    final_risk_multiplier = (
        base_risk_multiplier
        * council_multiplier
        * regime_multiplier
        * quantum_state.risk_multiplier
    )
    final_risk_multiplier = clamp(0.50, 1.25)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from math import log2
from typing import Dict, List, Optional


REGIMES = (
    "trend_up",
    "trend_down",
    "mean_revert",
    "panic",
    "squeeze",
    "neutral",
)

DOCTRINE_TEXT = (
    "quantum_state may reduce or modestly modulate risk. "
    "quantum_state may NOT change HOLD into BUY/SELL/SHORT. "
    "quantum_state may NOT reverse direction."
)


@dataclass
class BrainOpinion:
    brain: str
    direction: str          # BUY | SELL | SHORT | HOLD
    confidence: float       # 0.0 - 1.0
    risk_bias: float = 1.0  # suggested risk multiplier (advisory)
    reason: str = ""


@dataclass
class QuantumStateVerdict:
    regime_probs: Dict[str, float]
    entropy: float
    hold_lock_detected: bool
    risk_multiplier: float
    exploration_bias: float
    notes: List[str]

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────── helpers ───────────────────────────

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _normalize(weights: Dict[str, float]) -> Dict[str, float]:
    total = sum(max(v, 0.0) for v in weights.values())
    if total <= 0:
        return {k: 1.0 / len(weights) for k in weights}
    return {k: max(v, 0.0) / total for k, v in weights.items()}


def _entropy(probs: Dict[str, float]) -> float:
    """Shannon entropy normalized to [0, 1]. 0 = certainty (one regime
    dominates); 1 = maximum uncertainty (uniform spread)."""
    n = len(probs)
    if n <= 1:
        return 0.0
    raw = 0.0
    for p in probs.values():
        if p > 0:
            raw -= p * log2(p)
    return raw / log2(n)


# ─────────────────────────── builder ───────────────────────────

def build_quantum_inspired_state(
    opinions: List[BrainOpinion],
    market_features: Optional[dict] = None,
    *,
    min_risk: float = 0.50,
    max_risk: float = 1.25,
    hold_lock_entropy_floor: float = 0.35,
) -> QuantumStateVerdict:
    """Build the regime probability field and risk modulation.

    DOCTRINE (enforced by construction, not by guard rails):
      - Does NOT choose BUY/SELL/SHORT.
      - Does NOT promote HOLD into a trade.
      - Only produces regime probabilities, entropy, and bounded
        risk modulation.
    """
    market_features = market_features or {}
    notes: List[str] = []
    weights = {r: 1.0 for r in REGIMES}

    # ── Market-feature nudges ──────────────────────────────────
    momentum = float(market_features.get("momentum", 0.0))
    volatility = float(market_features.get("volatility", 0.0))
    rsi = float(market_features.get("rsi", 50.0))
    volume_z = float(market_features.get("volume_z", 0.0))

    if momentum > 0.02:
        weights["trend_up"] += 2.0
    elif momentum < -0.02:
        weights["trend_down"] += 2.0

    if volatility > 0.04:
        weights["panic"] += 1.5

    if rsi > 70:
        weights["mean_revert"] += 1.0
    elif rsi < 30:
        weights["squeeze"] += 1.0

    if volume_z > 2.0:
        weights["squeeze"] += 1.0
        weights["panic"] += 0.5

    # ── Brain-opinion nudges ───────────────────────────────────
    hold_count = 0
    actionable_count = 0

    for op in opinions:
        conf = _clamp(float(op.confidence), 0.0, 1.0)
        direction = (op.direction or "").upper().strip()

        if direction == "HOLD":
            hold_count += 1
            weights["neutral"] += 1.0 * conf
            continue

        actionable_count += 1

        if direction == "BUY":
            weights["trend_up"] += 2.0 * conf
            weights["squeeze"] += 0.5 * conf
        elif direction in {"SELL", "SHORT"}:
            weights["trend_down"] += 2.0 * conf
            weights["panic"] += 0.7 * conf

    probs = _normalize(weights)
    entropy = _entropy(probs)

    # ── HOLD-lock detection ────────────────────────────────────
    # When (almost) every brain says HOLD, the council collapses into
    # passive neutrality — and the regime field becomes degenerate (low
    # entropy because everything piled into `neutral`). Surface this so
    # the operator and the outcome learner can see it.
    hold_lock_detected = (
        hold_count >= max(2, len(opinions) - 1)
        and actionable_count == 0
        and entropy < hold_lock_entropy_floor
    )

    if hold_lock_detected:
        notes.append("HOLD_LOCK_DETECTED")
        exploration_bias = 0.15
    else:
        exploration_bias = 0.0

    # ── Risk modulation (NOT direction) ────────────────────────
    panic = probs.get("panic", 0.0)
    trend_up = probs.get("trend_up", 0.0)
    trend_down = probs.get("trend_down", 0.0)
    neutral = probs.get("neutral", 0.0)

    risk = 1.0
    risk -= panic * 0.45
    risk -= neutral * 0.25
    if trend_up > 0.35 or trend_down > 0.35:
        risk += 0.10

    # Direction-disagreement compression (the brains disagree on the
    # SIGN of the trade — not just hold/act). Size shrinks; direction
    # is left to the council.
    dirs = {(op.direction or "").upper().strip() for op in opinions}
    if len(dirs - {"HOLD"}) > 1:
        risk *= 0.85
        notes.append("DIRECTION_DISAGREEMENT_COMPRESSED")

    risk = _clamp(risk, min_risk, max_risk)

    return QuantumStateVerdict(
        regime_probs=probs,
        entropy=round(entropy, 4),
        hold_lock_detected=hold_lock_detected,
        risk_multiplier=round(risk, 4),
        exploration_bias=round(exploration_bias, 4),
        notes=notes,
    )
