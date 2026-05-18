"""Sovereign AI core — RISEDUAL doctrine, seat-governed.

The brain decides; MC regulates at the execution gate. Three doctrine
patches over the original:

  1. LIVE_TRADING_ENABLED defaults to True. The brain may propose a
     live order; whether it actually routes to a broker is decided by
     MC's seat policy + execution-gate chain. The brain has zero
     regulatory authority over its own orders — MC is the regulator.
  2. DB writes are LOCAL to the brain's host. The brain talks to MC
     via the runtime ingest API only.
  3. `execute_trade()` builds an order intent and hands it to MC's
     /api/execution/submit endpoint. MC's gate chain accepts or
     refuses. The brain does not pre-filter its own intents.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


# Brain self-declared live-trading posture. MC observes this; the seat
# policy + execution gate on the MC side is the only authority on what
# actually fires. Flipping this constant does NOT bypass MC.
LIVE_TRADING_ENABLED = True

MAX_NOTIONAL_PER_TRADE = 0.10
FEATURES = ["trend", "macd", "rsi"]

DEFAULT_WEIGHT = 0.5
MIN_WEIGHT = -3.0
MAX_WEIGHT = 3.0
LEARNING_RATE = 0.05


@dataclass(frozen=True)
class AdaptiveDecision:
    symbol: str
    action: str
    confidence: float
    notional: float
    features: dict[str, float]
    weights_snapshot: dict[str, float]
    created_at: str
    resolved: bool = False
    # Provenance — fed back into MC as `confidence_origin` /
    # `memory_sources` on the corresponding stance.
    confidence_origin: dict[str, float] = field(default_factory=dict)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def sigmoid(x: float) -> float:
    if x >= 50:
        return 1.0
    if x <= -50:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def build_features(top: dict) -> dict[str, float]:
    """Convert raw market state into normalized -1/0/+1 features.

    `top` is the brain's view of a symbol — passed in by the sidecar so
    the core doesn't depend on any specific DB schema."""
    tech = top.get("technicals", {}) or {}

    price = safe_float(top.get("price"))
    sma20 = safe_float(tech.get("sma20"))
    macd = safe_float(tech.get("macd"))
    rsi = safe_float(tech.get("rsi14"), 50.0)

    trend = 0.0
    # Doctrine: missing SMA must NOT create a fake bullish signal.
    if price > 0 and sma20 > 0:
        trend = 1.0 if price > sma20 else -1.0

    macd_signal = 0.0
    if macd > 0:
        macd_signal = 1.0
    elif macd < 0:
        macd_signal = -1.0

    rsi_signal = 0.0
    if rsi > 55:
        rsi_signal = 1.0
    elif rsi < 45:
        rsi_signal = -1.0

    return {"trend": trend, "macd": macd_signal, "rsi": rsi_signal}


def default_weights() -> dict[str, float]:
    return {f: DEFAULT_WEIGHT for f in FEATURES}


def normalize_weights(raw: dict | None) -> dict[str, float]:
    raw = raw or {}
    return {
        f: clamp(safe_float(raw.get(f), DEFAULT_WEIGHT), MIN_WEIGHT, MAX_WEIGHT)
        for f in FEATURES
    }


def compute_score(features: dict[str, float], weights: dict[str, float]) -> tuple[float, dict[str, float]]:
    """Returns (sigmoid_score, per-feature contribution map).

    The contribution map is what we ship to MC as `confidence_origin`."""
    contributions: dict[str, float] = {}
    raw = 0.0
    for f in FEATURES:
        w = safe_float(weights.get(f), DEFAULT_WEIGHT)
        s = safe_float(features.get(f), 0.0)
        contributions[f] = round(w * s, 4)
        raw += w * s
    return sigmoid(raw), contributions


def decide_from_score(score: float) -> tuple[str, float]:
    if score > 0.55:
        return "BUY", score
    if score < 0.45:
        return "SELL", 1.0 - score
    return "HOLD", 0.5


def compute_notional(action: str, confidence: float, account_size: float) -> float:
    if action == "HOLD":
        return 0.0
    account_size = max(0.0, safe_float(account_size))
    return round(confidence * MAX_NOTIONAL_PER_TRADE * account_size, 2)


def assert_safe_action(action: str) -> None:
    """Belt-and-suspenders: the action must be one of the three allowed
    values. Phase 1 enforces this in MC's API too, but check at the core
    so a corrupted intermediate value cannot reach the sidecar."""
    if action not in {"BUY", "SELL", "HOLD"}:
        raise RuntimeError(f"sovereign_core: refused unsafe action {action!r}")


def map_action_to_stance(action: str) -> str:
    """Adapter: RISEDUAL's stance vocabulary is long/short/abstain."""
    return {"BUY": "long", "SELL": "short", "HOLD": "abstain"}[action]


def run_adaptive_core(
    top: dict, weights: dict[str, float], account_size: float = 0.0,
) -> AdaptiveDecision:
    """Pure decision step. Caller (sidecar) handles persistence + POST.

    `top` is `{symbol, price, technicals: {sma20, macd, rsi14}}`.
    `weights` is the brain's current weights dict.
    Returns AdaptiveDecision; does NOT mutate weights and does NOT call
    execute_trade."""
    symbol = (top.get("symbol") or "").upper().strip()
    features = build_features(top)
    weights_snapshot = normalize_weights(weights)
    score, contributions = compute_score(features, weights_snapshot)
    action, confidence = decide_from_score(score)
    assert_safe_action(action)
    notional = compute_notional(action, confidence, account_size)

    return AdaptiveDecision(
        symbol=symbol,
        action=action,
        confidence=round(confidence, 4),
        notional=notional,
        features=dict(features),
        weights_snapshot=weights_snapshot,
        created_at=utcnow(),
        resolved=False,
        confidence_origin=contributions,
    )


def update_weights(
    weights: dict[str, float], features: dict[str, float],
    outcome: int, lr: float = LEARNING_RATE,
) -> dict[str, float]:
    """Pure: returns the new weights dict; caller persists."""
    out = normalize_weights(weights)
    for f in FEATURES:
        signal = safe_float(features.get(f), 0.0)
        out[f] = clamp(out[f] + lr * outcome * signal, MIN_WEIGHT, MAX_WEIGHT)
    return out


def execute_trade(symbol: str, action: str, notional: float) -> dict:
    """Build an order intent payload. The brain hands this to MC via
    `MCClient.submit_order_intent()`; MC's seat-policy + execution-gate
    chain decides whether to route to a broker. The brain does NOT
    self-regulate — pre-filtering its own intents was the old
    observation-only doctrine; that's been removed.

    Returns the intent envelope; the caller is responsible for shipping
    it to MC and recording the gate result locally.
    """
    return {
        "intent": True,
        "symbol": symbol,
        "action": action,
        "notional": notional,
        # Default False — set True by the caller after MC's gate returns
        # would_pass + the broker confirms submission.
        "executed": False,
        "reason": "intent built; awaiting MC execution gate",
        "ts": utcnow(),
    }


__all__ = [
    "AdaptiveDecision", "FEATURES",
    "DEFAULT_WEIGHT", "MIN_WEIGHT", "MAX_WEIGHT", "LEARNING_RATE",
    "default_weights", "normalize_weights",
    "build_features", "compute_score", "decide_from_score",
    "compute_notional", "run_adaptive_core", "update_weights",
    "assert_safe_action", "map_action_to_stance",
    "execute_trade", "LIVE_TRADING_ENABLED",
    "asdict",
]
