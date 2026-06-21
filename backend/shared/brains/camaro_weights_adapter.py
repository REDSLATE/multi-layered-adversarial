"""Barracuda × Camaro-weights integration adapter (2026-02-21).

Operator pin:
    "I want to use the Camaro wrap for Barracuda-only."
    "Camaro was more unrestricted than any of the others. And it
    was paying off until the update crash."

Architectural intent:
    Barracuda is currently configured to run through
    `apply_camaro_legacy_strategist` (see legacy_brain_wrappers.py).
    The operator brought in an upgraded `camaro_weights.py` decision
    engine — sizing bands, regime-aware RR floors, graduated loss
    streak dampening, leader-penalty scaling, conviction score, and
    explicit vetoes (RISK_BLOCK, LOW_RR, EVENT_RISK_RESTRICTED).
    Those vetoes are the missing safety net that hit Camaro harder
    than Alpha during the last update crash.

    This adapter sits as a PRE-PASS before the legacy Camaro wrapper:
      1. Extract council/regime/risk inputs from the intent envelope.
      2. Call `build_weighted_decision(...)` to produce the
         authoritative WeightedDecision.
      3. Overwrite intent confidence with the weighted confidence.
      4. Multiply intent size_bias by the weighted size_multiplier
         (preserves upstream sizing intent, then layers the
         band-based authority on top).
      5. Append vetoes to warnings.
      6. Stamp full decision into `evidence.camaro_weights` for
         audit & UI surfacing.

    The existing legacy wrapper then runs ON TOP, applying its
    position-aware refinements (FLIP penalties, current_side
    continuation).

    Note on terminology: titles ("strategist", "executor",
    "governor", "auditor") belong to SEATS, not brains. Brains
    are cognitive temperaments routed into seats by lane policy.
    This module is bound to the brain identity `barracuda` — not
    to any title.

Kill switch: `RISEDUAL_BARRACUDA_CAMARO_WEIGHTS_DISABLED=1` env var.
Failure mode: fail-soft. Any exception stamps `evidence.
camaro_weights_error` and lets the legacy wrapper run on the
original confidence — Camaro's looseness is preserved by design,
this adapter must not introduce new ways to silently drop trades.
"""
from __future__ import annotations

import dataclasses
import os
from typing import Any

from shared.brains.camaro_weights import (
    EventRisk, Regime, build_weighted_decision,
)


# ── Regime mapping ──────────────────────────────────────────────────
# Intent envelopes use a wider regime vocabulary than camaro_weights'
# four canonical values. Map sympathetically; anything we don't
# recognise falls back to NEUTRAL (the baseline) so Camaro doesn't
# silently treat a chop regime as a bull regime.


_REGIME_ALIASES: dict[str, Regime] = {
    # Bull family
    "bull": Regime.BULL,
    "calm_bull": Regime.BULL,
    "risk_on": Regime.BULL,
    "trend_up": Regime.BULL,
    "trending": Regime.BULL,  # ambiguous but bias bull when bullish indicators dominate
    # Bear family
    "bear": Regime.BEAR,
    "crisis": Regime.BEAR,
    "risk_off": Regime.BEAR,
    "trend_down": Regime.BEAR,
    # High-vol family
    "high_vol": Regime.HIGH_VOL,
    "parabolic": Regime.HIGH_VOL,
    "overbought": Regime.HIGH_VOL,
    "oversold": Regime.HIGH_VOL,
    # Neutral family
    "neutral": Regime.NEUTRAL,
    "chop": Regime.NEUTRAL,
    "sideways": Regime.NEUTRAL,
    "range": Regime.NEUTRAL,
    "uncertain": Regime.NEUTRAL,
    "unknown": Regime.NEUTRAL,
}


_EVENT_RISK_ALIASES: dict[str, EventRisk] = {
    "normal": EventRisk.NORMAL,
    "elevated": EventRisk.ELEVATED,
    "restricted": EventRisk.RESTRICTED,
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_regime(raw: Any) -> Regime:
    if isinstance(raw, Regime):
        return raw
    key = str(raw or "").strip().lower()
    return _REGIME_ALIASES.get(key, Regime.NEUTRAL)


def _coerce_event_risk(raw: Any) -> EventRisk:
    if isinstance(raw, EventRisk):
        return raw
    key = str(raw or "").strip().lower()
    return _EVENT_RISK_ALIASES.get(key, EventRisk.NORMAL)


def _default_vote_counts(action: str) -> dict[str, int]:
    """When no upstream council exists, fabricate a clean single-vote
    proxy from the intent's action so the council math doesn't fire
    spurious `no_quorum` penalties on intents that simply don't have
    a council yet. This preserves Camaro's "looseness" — no council
    data should not be treated as `no_quorum` disagreement.
    """
    a = (action or "").upper()
    if a == "BUY":
        return {"bull": 1}
    if a == "SELL":
        return {"bear": 1}
    return {}


def kill_switch_tripped() -> bool:
    """Env-var kill switch — sync, no DB call."""
    v = os.environ.get(
        "RISEDUAL_BARRACUDA_CAMARO_WEIGHTS_DISABLED", "",
    ).strip().lower()
    return v in {"1", "true", "yes", "on"}


def apply_camaro_weights_to_intent(intent: dict[str, Any]) -> dict[str, Any]:
    """Pre-pass that runs `build_weighted_decision` on a Barracuda
    intent and writes the authoritative outputs back to the envelope.

    Mutates `intent` in place AND returns it. Fail-soft: on any
    exception the original intent is returned with
    `evidence.camaro_weights_error` stamped.
    """
    try:
        ev = intent.get("evidence") or {}
        action = str(intent.get("action") or "HOLD").upper()
        # Direction: bull/bear/neutral, derived from action.
        direction = (
            "bull" if action == "BUY"
            else "bear" if action == "SELL"
            else "neutral"
        )

        # Inputs — pull from evidence with sensible Camaro-friendly
        # defaults that preserve looseness when upstream data is
        # missing. The only ways Camaro should be MORE restricted
        # than today are when evidence ACTIVELY says so.
        raw_confidence = _safe_float(intent.get("confidence"), 0.5)
        bull_score = _safe_float(ev.get("buy_score"), raw_confidence if direction == "bull" else 0.0)
        bear_score = _safe_float(ev.get("sell_score"), raw_confidence if direction == "bear" else 0.0)
        risk_prob = _safe_float(ev.get("risk_prob"), 0.0)
        edge_gap = _safe_float(ev.get("edge_gap"), abs(bull_score - bear_score))
        strategist_score = ev.get("strategist_score")
        if strategist_score is not None:
            strategist_score = _safe_float(strategist_score, 0.0)

        vote_counts = ev.get("vote_counts")
        if not isinstance(vote_counts, dict) or not vote_counts:
            vote_counts = _default_vote_counts(action)

        regime = _coerce_regime(ev.get("market_regime") or ev.get("regime"))
        regime_conf = _safe_float(ev.get("regime_conf"), 0.6)
        loss_streak = _safe_int(ev.get("loss_streak"), 0)
        event_risk = _coerce_event_risk(ev.get("event_risk"))
        # rr_ratio default = MIN_RR_BASE (1.50) so missing data
        # doesn't trip LOW_RR vetoes on intents that legitimately
        # didn't carry an RR estimate yet.
        rr_ratio = _safe_float(
            ev.get("rr_ratio") or ev.get("risk_reward_ratio"),
            1.50,
        )

        weighted = build_weighted_decision(
            action=action,
            direction=direction,
            raw_confidence=raw_confidence,
            bull_score=bull_score,
            bear_score=bear_score,
            risk_prob=risk_prob,
            vote_counts=vote_counts,
            strategist_score=strategist_score,
            edge_gap=edge_gap,
            regime=regime,
            regime_conf=regime_conf,
            loss_streak=loss_streak,
            event_risk=event_risk,
            rr_ratio=rr_ratio,
        )

        # Writeback — confidence becomes authoritative; size_bias
        # is MULTIPLIED so upstream sizing intent is preserved.
        intent["confidence"] = weighted.confidence
        prior_size_bias = _safe_float(intent.get("size_bias"), 1.0)
        intent["size_bias"] = round(prior_size_bias * weighted.size_multiplier, 4)

        # Vetoes graduate to warnings.
        warnings = intent.get("warnings") or []
        for v in weighted.vetoes:
            if v not in warnings:
                warnings.append(v)
        intent["warnings"] = warnings

        # Full decision into evidence for audit / UI surfacing.
        # `dataclasses.asdict` collapses the enums to their str
        # values via the `str, Enum` mixin — JSON-safe.
        ev["camaro_weights"] = dataclasses.asdict(weighted)
        # Pull a few key fields up to the top level of evidence for
        # easy UI access.
        ev["conviction_score"] = weighted.conviction_score
        ev["camaro_band"] = str(weighted.band.value)
        ev["camaro_size_multiplier"] = weighted.size_multiplier
        intent["evidence"] = ev

    except Exception as exc:  # noqa: BLE001
        # Fail-soft. Stamp the error and return the unmodified
        # intent so the legacy wrapper can still run.
        ev = intent.setdefault("evidence", {})
        ev["camaro_weights_error"] = repr(exc)

    return intent
