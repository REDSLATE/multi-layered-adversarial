"""Alpha decision engine — adversarial Bull/Bear kernel (2026-02-21).

A faithful port of the operator's standalone Alpha kernel into the
RISEDUAL package. Stateless, pure functions — no Mongo, no async, no
network. Lives at `shared/brains/alpha_engine.py` so any brain
(Camino, Barracuda, Hellcat, GTO) can reuse it.

Why two agents?
    Consensus systems (Strategist + Auditor) share an objective, so
    when both agree and the trade fails you can't tell which one was
    wrong. Bull vs Bear gives unambiguous ground truth after the
    trade resolves — the loser is wrong by construction. This is
    exactly what `shared_brain_outcomes` and the report-card stack
    want for training data.

Toxic-spike seal:
    Confidence is hard-capped at 0.95. A saturated 1.0 trade would
    weight setup_memory infinitely and create feedback loops. The
    cap is universal and applies even when narrative enrichment
    would otherwise push higher.

Public surface:
    * `AlphaConfig` — every tunable in one frozen dataclass.
    * `AgentOutput` — Bull/Bear emit shape.
    * `cap_confidence(value, cap=0.95)` — the toxic-spike seal.
    * `bull_agent(signal, cfg)` / `bear_agent(signal, cfg)`.
    * `apply_options_context(...)` / `apply_catalyst_context(...)`
      — additive narrative, never moves scores, only annotates thesis.
    * `resolve_adversarial(bull, bear, cfg)` — the Commander.
    * `Alpha` — orchestrator class with `.decide(signal)`.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


# ── Tunables ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AlphaConfig:
    edge_gap_threshold: float = 0.35
    risk_multiplier_cap: float = 1.0
    confidence_cap: float = 0.95
    pcr_bull_threshold: float = 0.7
    pcr_bear_threshold: float = 1.3
    min_confidence_floor: float = 0.55


@dataclass
class AgentOutput:
    side: str
    confidence: float
    expected_r: float
    thesis: str
    invalidations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ── Toxic-spike seal ─────────────────────────────────────────────────


def cap_confidence(value: float, *, cap: float = 0.95) -> float:
    """Hard-cap any confidence at `cap`. Accepts 0–1 or 0–100 scales."""
    if value is None:
        return 0.0
    v = float(value)
    ceiling = cap * 100.0 if v > 1.0 else cap
    if v >= ceiling:
        return ceiling
    if v < 0.0:
        return 0.0
    return v


# ── Input normalisation ──────────────────────────────────────────────


_VOL_BY_REGIME = {
    "parabolic": 0.85, "overbought": 0.75, "oversold": 0.75,
    "trending": 0.50,  "trend_up": 0.50,   "trend_down": 0.50,
    "range": 0.30,     "uncertain": 0.40,
}


def _extract_inputs(signal: dict[str, Any]) -> dict[str, float]:
    strategist = signal.get("strategist") or {}
    indicators = strategist.get("indicators") or {}
    rsi = float(indicators.get("rsi") or 50.0)
    momentum_raw = float(indicators.get("momentum_5b") or 0.0)
    regime = (signal.get("regime") or "").lower()
    auditor = signal.get("auditor") or {}

    rsi_norm = max(0.0, min(rsi / 100.0, 1.0))
    momentum_signed = math.tanh(momentum_raw * 20.0)
    momentum_unit = (momentum_signed + 1.0) / 2.0
    trend_proxy = float(strategist.get("confidence") or 0.5)
    volatility = _VOL_BY_REGIME.get(regime, 0.40)

    return {
        "rsi": rsi_norm,
        "momentum_signed": momentum_signed,
        "momentum_unit": momentum_unit,
        "trend": trend_proxy,
        "volatility": volatility,
        "auditor_conf": float(auditor.get("confidence") or 0.5),
        "regime": regime,
    }


# ── Bull / Bear agents ───────────────────────────────────────────────


def bull_agent(signal: dict[str, Any], *, cfg: AlphaConfig) -> AgentOutput:
    inputs = _extract_inputs(signal)
    raw = 0.30 + 0.40 * inputs["momentum_unit"] + 0.30 * inputs["trend"]
    if inputs["rsi"] > 0.70:
        raw -= 0.30 * (inputs["rsi"] - 0.70) / 0.30
    confidence = cap_confidence(max(0.0, min(raw, 1.0)), cap=cfg.confidence_cap)
    expected_r = 1.0 + 0.5 * abs(inputs["momentum_signed"])
    return AgentOutput(
        side="LONG",
        confidence=confidence,
        expected_r=round(expected_r, 4),
        thesis="momentum_plus_trend",
        invalidations=["rsi_divergence", "volume_drop", "regime_break"],
    )


def bear_agent(signal: dict[str, Any], *, cfg: AlphaConfig) -> AgentOutput:
    inputs = _extract_inputs(signal)
    raw = 0.30 + 0.40 * (inputs["rsi"] - 0.30) + 0.20 * inputs["volatility"]
    raw -= 0.30 * inputs["momentum_signed"]
    if inputs["rsi"] > 0.70 and inputs["volatility"] > 0.70:
        raw += 0.15
    confidence = cap_confidence(max(0.0, min(raw, 1.0)), cap=cfg.confidence_cap)
    expected_r = 1.0 + 0.5 * inputs["volatility"]
    return AgentOutput(
        side="SHORT_OR_REJECT",
        confidence=confidence,
        expected_r=round(expected_r, 4),
        thesis="overbought_plus_volatility",
        invalidations=["strong_continuation", "volume_expansion_up"],
    )


# ── Narrative enrichment ─────────────────────────────────────────────


def apply_options_context(
    bull: AgentOutput, bear: AgentOutput,
    options_entry: Optional[dict[str, Any]], *, cfg: AlphaConfig,
) -> tuple[AgentOutput, AgentOutput]:
    if not options_entry:
        return bull, bear
    agg = options_entry.get("aggregate") or {}
    pcr = agg.get("put_call_ratio")
    stress = agg.get("liquidity_stress_index")
    if pcr is not None:
        if pcr < cfg.pcr_bull_threshold:
            bull = dataclasses.replace(bull, thesis=bull.thesis + " | strong_call_flow")
        elif pcr > cfg.pcr_bear_threshold:
            bear = dataclasses.replace(bear, thesis=bear.thesis + " | elevated_put_activity")
    if stress is not None:
        try:
            v = float(stress)
        except (TypeError, ValueError):
            v = 0.0
        if v >= 6.0:
            bear = dataclasses.replace(bear, thesis=bear.thesis + " | liquidity_instability_imminent")
        elif v >= 4.0:
            bear = dataclasses.replace(bear, thesis=bear.thesis + " | liquidity_stress_rising")
    return bull, bear


def apply_catalyst_context(
    bull: AgentOutput, bear: AgentOutput,
    catalyst: Optional[dict[str, Any]],
) -> tuple[AgentOutput, AgentOutput]:
    if not catalyst:
        return bull, bear
    shock = catalyst.get("news_shock", {}) or {}
    sent = shock.get("sentiment_label")
    state = shock.get("shock_state")
    z = shock.get("news_zscore")
    if sent == "bullish":
        bull = dataclasses.replace(bull, thesis=bull.thesis + " | bullish_catalyst_sentiment")
    elif sent == "bearish":
        bear = dataclasses.replace(bear, thesis=bear.thesis + " | bearish_catalyst_sentiment")
    if state in {"elevated", "high"}:
        tag = f"news_shock_{state}" + (f"_z{z}" if z is not None else "")
        bull = dataclasses.replace(bull, thesis=bull.thesis + f" | {tag}")
        bear = dataclasses.replace(bear, thesis=bear.thesis + f" | {tag}")
    return bull, bear


# ── Commander ────────────────────────────────────────────────────────


def resolve_adversarial(
    bull: AgentOutput, bear: AgentOutput, *, cfg: AlphaConfig,
) -> dict[str, Any]:
    bull_score = bull.confidence * bull.expected_r
    bear_score = bear.confidence * bear.expected_r
    edge_gap = bull_score - bear_score
    if edge_gap > cfg.edge_gap_threshold:
        decision, winning_conf = "LONG", bull.confidence
    elif edge_gap < -cfg.edge_gap_threshold:
        decision, winning_conf = "SHORT_OR_AVOID", bear.confidence
    else:
        decision, winning_conf = "NO_TRADE", max(bull.confidence, bear.confidence)
    if decision != "NO_TRADE" and winning_conf < cfg.min_confidence_floor:
        decision = "NO_TRADE"
    risk_multiplier = max(0.0, min(abs(edge_gap), cfg.risk_multiplier_cap))
    return {
        "decision": decision,
        "edge_gap": round(edge_gap, 4),
        "risk_multiplier": round(risk_multiplier, 4),
        "bull_score": round(bull_score, 4),
        "bear_score": round(bear_score, 4),
        "confidence": round(cap_confidence(winning_conf, cap=cfg.confidence_cap), 4),
    }


# ── Orchestrator ─────────────────────────────────────────────────────


class Alpha:
    """Stateless. Every `.decide()` call is independent."""

    def __init__(self, config: Optional[AlphaConfig] = None) -> None:
        self.cfg = config or AlphaConfig()

    def decide(self, signal: dict[str, Any]) -> dict[str, Any]:
        bull = bull_agent(signal, cfg=self.cfg)
        bear = bear_agent(signal, cfg=self.cfg)
        bull, bear = apply_options_context(bull, bear, signal.get("options"), cfg=self.cfg)
        bull, bear = apply_catalyst_context(bull, bear, signal.get("catalyst"))
        verdict = resolve_adversarial(bull, bear, cfg=self.cfg)
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "decision": verdict["decision"],
            "confidence": verdict["confidence"],
            "size_fraction": verdict["risk_multiplier"],
            "edge_gap": verdict["edge_gap"],
            "bull": bull.as_dict(),
            "bear": bear.as_dict(),
            "thesis": (
                bull.thesis if verdict["decision"] == "LONG"
                else bear.thesis if verdict["decision"] == "SHORT_OR_AVOID"
                else "no_edge"
            ),
            "config": dataclasses.asdict(self.cfg),
        }
