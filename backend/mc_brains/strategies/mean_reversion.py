"""Barracuda's strategy: mean reversion.

Doctrine: FADE extremes. When the market has stretched too far in
one direction, Barracuda takes the OTHER side. This is the ONLY
brain that reliably disagrees with a strong trend — its natural
counter-position gives the council a genuine contrarian voice.

Primary features:
  * `rsi` — the classic overbought/oversold signal.
  * `vwap_distance_pct` — how far price has drifted from the
    session's fair-value anchor.

Barracuda's distinctness: it is the ONLY brain whose direction
inversely correlates with strong price moves. When Camino/GTO
are shouting BUY on a strong uptrend and Hellcat is checking
execution, Barracuda is looking at RSI=82 and saying "the trend
is exhausted."
"""
from __future__ import annotations

from typing import ClassVar

from mc_brains.strategies import StrategyResult
from mc_pulse.snapshot import MarketSnapshot


class MeanReversionStrategy:
    """Barracuda's strategy — see module docstring."""

    reason_code_family: ClassVar[str] = "MEAN"

    # Classic RSI thresholds. Barracuda uses tighter bands than
    # textbook (30/70) — its opportunistic personality prefers
    # firing at 35/65 and letting the extremes reinforce.
    RSI_OVERSOLD = 35.0
    RSI_OVERBOUGHT = 65.0
    RSI_EXTREME_OVERSOLD = 25.0
    RSI_EXTREME_OVERBOUGHT = 75.0

    # VWAP-distance thresholds — magnitude in percent.
    VWAP_EXTENSION_MAG = 2.0
    VWAP_EXTREME_MAG = 4.0

    def evaluate(self, snapshot: MarketSnapshot) -> StrategyResult:
        feats = snapshot.feature_snapshot or {}
        rsi = _f(feats.get("rsi"))
        vwap_d = _f(feats.get("vwap_distance_pct"))

        if rsi is None and vwap_d is None:
            return StrategyResult(
                action="HOLD", confidence=0.0,
                reason_codes=("MEAN_NO_SIGNAL",),
                edge_evidence={"rsi": None, "vwap_distance_pct": None},
            )

        # RSI voting.
        rsi_vote = 0    # -1 SELL (overbought), +1 BUY (oversold)
        rsi_extreme = False
        rsi_reason: str | None = None
        if rsi is not None:
            if rsi >= self.RSI_EXTREME_OVERBOUGHT:
                rsi_vote = -1
                rsi_extreme = True
                rsi_reason = "MEAN_RSI_EXTREME_OVERBOUGHT"
            elif rsi >= self.RSI_OVERBOUGHT:
                rsi_vote = -1
                rsi_reason = "MEAN_RSI_OVERBOUGHT"
            elif rsi <= self.RSI_EXTREME_OVERSOLD:
                rsi_vote = 1
                rsi_extreme = True
                rsi_reason = "MEAN_RSI_EXTREME_OVERSOLD"
            elif rsi <= self.RSI_OVERSOLD:
                rsi_vote = 1
                rsi_reason = "MEAN_RSI_OVERSOLD"

        # VWAP-distance voting (positive = above vwap, likely to
        # revert down; negative = below vwap, likely to revert up).
        vwap_vote = 0
        vwap_extreme = False
        vwap_reason: str | None = None
        if vwap_d is not None:
            if vwap_d >= self.VWAP_EXTREME_MAG:
                vwap_vote = -1
                vwap_extreme = True
                vwap_reason = "MEAN_VWAP_EXTENSION_EXTREME_HIGH"
            elif vwap_d >= self.VWAP_EXTENSION_MAG:
                vwap_vote = -1
                vwap_reason = "MEAN_VWAP_EXTENSION_HIGH"
            elif vwap_d <= -self.VWAP_EXTREME_MAG:
                vwap_vote = 1
                vwap_extreme = True
                vwap_reason = "MEAN_VWAP_EXTENSION_EXTREME_LOW"
            elif vwap_d <= -self.VWAP_EXTENSION_MAG:
                vwap_vote = 1
                vwap_reason = "MEAN_VWAP_EXTENSION_LOW"

        # Normal range → HOLD.
        if rsi_vote == 0 and vwap_vote == 0:
            return StrategyResult(
                action="HOLD", confidence=0.25,
                reason_codes=("MEAN_NORMAL_RANGE",),
                edge_evidence={"rsi": rsi, "vwap_distance_pct": vwap_d},
            )

        # Conflict → HOLD (rare but real).
        if rsi_vote != 0 and vwap_vote != 0 and rsi_vote != vwap_vote:
            return StrategyResult(
                action="HOLD", confidence=0.30,
                reason_codes=("MEAN_SIGNALS_CONFLICT",
                              rsi_reason or "", vwap_reason or ""),
                edge_evidence={"rsi": rsi, "vwap_distance_pct": vwap_d,
                               "rsi_vote": rsi_vote, "vwap_vote": vwap_vote},
            )

        # Aligned (either RSI alone, VWAP alone, or both agreeing).
        direction = rsi_vote or vwap_vote
        both = rsi_vote != 0 and vwap_vote != 0
        extreme_hit = rsi_extreme or vwap_extreme

        base = 0.55
        if both:
            base += 0.10
        if extreme_hit:
            base += 0.10
        conf = min(0.90, base)

        reasons = tuple(r for r in (rsi_reason, vwap_reason) if r)
        family_tail = ("MEAN_SIGNALS_AGREE",) if both else ()

        action = "BUY" if direction > 0 else "SELL"
        return StrategyResult(
            action=action, confidence=round(conf, 4),
            reason_codes=reasons + family_tail,
            edge_evidence={"rsi": rsi, "vwap_distance_pct": vwap_d,
                           "both_signals": both, "extreme": extreme_hit},
        )


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
