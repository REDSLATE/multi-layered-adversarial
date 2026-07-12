"""Camino's strategy: trend following.

Doctrine: identify + confirm the DIRECTION of an existing trend.
Primary feature: `trend_score` (signed strength). Confirmation:
`price_change_pct` matches trend sign. Regime gate: boost when
`market_regime == "trending"`.

Camino does NOT fade extremes (that's Barracuda's job), does
NOT require multi-signal alignment (that's GTO's), does NOT
gate on execution quality (that's Hellcat's). Its distinctness
comes from being the ONLY brain that emits opinions on weak
trend evidence — the trend follower is willing to be early.
"""
from __future__ import annotations

from typing import ClassVar

from mc_brains.strategies import StrategyResult
from mc_pulse.snapshot import MarketSnapshot


class TrendFollowingStrategy:
    """Camino's strategy — see module docstring."""

    reason_code_family: ClassVar[str] = "TREND"

    # Thresholds — tuned to Camino's "willing to be early" doctrine.
    # These stay module-level constants (not instance state) so the
    # strategy remains stateless + trivially replay-testable.
    TREND_ENTRY_MAG = 0.20        # |trend_score| ≥ this to open a call
    TREND_STRONG_MAG = 0.60       # |trend_score| ≥ this → high confidence
    PRICE_CONFIRM_MAG = 0.05      # |price_change_pct| ≥ this to confirm
    REGIME_BOOST = 0.10           # additive when market_regime==trending

    def evaluate(self, snapshot: MarketSnapshot) -> StrategyResult:
        feats = snapshot.feature_snapshot or {}
        trend = _f(feats.get("trend_score"))
        pc = _f(feats.get("price_change_pct"))
        regime = str(feats.get("market_regime") or snapshot.market_state or "unknown").lower()

        # Nothing to grade — bail with a family-specific reason.
        if trend is None:
            return StrategyResult(
                action="HOLD", confidence=0.0,
                reason_codes=("TREND_NO_SIGNAL",),
                edge_evidence={"trend_score": None},
            )

        # Below-entry threshold → HOLD with the "ranging" tag.
        if abs(trend) < self.TREND_ENTRY_MAG:
            return StrategyResult(
                action="HOLD", confidence=0.3,
                reason_codes=("TREND_WEAK", f"TREND_REGIME_{regime.upper()}"),
                edge_evidence={"trend_score": trend, "reason": "below_entry"},
            )

        want_up = trend > 0
        want_down = trend < 0

        # Confirmation: price must move in the trend's direction.
        pc_confirms = (
            pc is not None
            and ((want_up and pc >= self.PRICE_CONFIRM_MAG)
                 or (want_down and pc <= -self.PRICE_CONFIRM_MAG))
        )
        if not pc_confirms:
            return StrategyResult(
                action="HOLD", confidence=0.35,
                reason_codes=("TREND_UNCONFIRMED_BY_PRICE",),
                edge_evidence={
                    "trend_score": trend, "price_change_pct": pc,
                    "required_price_confirm_mag": self.PRICE_CONFIRM_MAG,
                },
            )

        # Confirmed. Confidence scales with |trend|.
        strength = min(1.0, abs(trend) / self.TREND_STRONG_MAG)
        conf = 0.40 + 0.40 * strength   # 0.40..0.80 baseline
        if regime == "trending":
            conf = min(1.0, conf + self.REGIME_BOOST)

        if want_up:
            return StrategyResult(
                action="BUY", confidence=round(conf, 4),
                reason_codes=(
                    "TREND_UP_CONFIRMED",
                    f"TREND_REGIME_{regime.upper()}",
                    "TREND_STRENGTH_STRONG" if strength >= 1.0 else "TREND_STRENGTH_MODERATE",
                ),
                edge_evidence={"trend_score": trend, "price_change_pct": pc,
                               "strength": round(strength, 3)},
            )
        return StrategyResult(
            action="SELL", confidence=round(conf, 4),
            reason_codes=(
                "TREND_DOWN_CONFIRMED",
                f"TREND_REGIME_{regime.upper()}",
                "TREND_STRENGTH_STRONG" if strength >= 1.0 else "TREND_STRENGTH_MODERATE",
            ),
            edge_evidence={"trend_score": trend, "price_change_pct": pc,
                           "strength": round(strength, 3)},
        )


def _f(v) -> float | None:
    """Coerce to float, silently return None on failure."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
