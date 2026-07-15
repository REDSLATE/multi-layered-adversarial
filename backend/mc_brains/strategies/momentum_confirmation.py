"""GTO's strategy: momentum confirmation.

Doctrine: DISCIPLINED — needs MULTIPLE momentum signals to
align in the same direction before firing. Refuses to trade on
a single indicator, no matter how strong. This is the "wait for
the whole picture" brain.

Primary features: price_change_pct, volume_change_pct,
relative_volume, gap_pct (for equity opens).

GTO's distinctness: it produces the HIGHEST HOLD rate of the
4 brains BY DESIGN. When it fires, it fires with high
confidence because 3+ signals aligned.
"""
from __future__ import annotations

from typing import ClassVar

from mc_brains.strategies import StrategyResult
from mc_pulse.snapshot import MarketSnapshot


class MomentumConfirmationStrategy:
    """GTO's strategy — see module docstring."""

    reason_code_family: ClassVar[str] = "MOMENTUM"

    # Thresholds tuned for GTO's disciplined stance.
    PRICE_MAG = 0.10              # |price_change_pct| >= this
    VOLUME_DELTA_MAG = 0.30       # volume_change_pct >= this (surge)
    RELATIVE_VOLUME_MIN = 1.20    # >= 1.2x rolling volume
    GAP_MAG = 0.30                # |gap_pct| >= this → gap confirm
    REQUIRED_CONFIRMS = 3         # need >=3 aligned signals to fire

    def evaluate(self, snapshot: MarketSnapshot) -> StrategyResult:
        feats = snapshot.feature_snapshot or {}
        # 2026-07-15 (iter-30 P2): GTO's "MOMENTUM" doctrine is
        # single-bar impulse confirmation — the bar-over-bar
        # semantic. Prefer the explicit field; fall back to
        # `price_change_pct` for backward compat during the roll-
        # forward window.
        pc = _f(feats.get("bar_change_pct"))
        if pc is None:
            pc = _f(feats.get("price_change_pct"))
        vc = _f(feats.get("volume_change_pct"))
        rv = _f(feats.get("relative_volume"))
        gap = _f(feats.get("gap_pct"))

        # Structural gate: need at least price_change to grade.
        if pc is None:
            return StrategyResult(
                action="HOLD", confidence=0.0,
                reason_codes=("MOMENTUM_NO_SIGNAL",),
                edge_evidence={"price_change_pct": None},
            )

        # Collect direction votes from each signal.
        # +1 = up-momentum vote, -1 = down-momentum vote, 0 = neutral.
        votes: dict[str, int] = {}

        # 1. Price change.
        if abs(pc) >= self.PRICE_MAG:
            votes["price"] = 1 if pc > 0 else -1
        else:
            votes["price"] = 0

        # 2. Volume surge (unsigned magnitude).
        if vc is not None and vc >= self.VOLUME_DELTA_MAG:
            # Volume surge amplifies the PRICE direction (a surge on
            # a rising bar is bullish; on a falling bar, bearish).
            votes["volume"] = votes["price"] if votes["price"] != 0 else 0
        else:
            votes["volume"] = 0

        # 3. Relative volume — same rule as volume surge.
        if rv is not None and rv >= self.RELATIVE_VOLUME_MIN:
            votes["rvol"] = votes["price"] if votes["price"] != 0 else 0
        else:
            votes["rvol"] = 0

        # 4. Gap alignment (equity opens; crypto ~0).
        if gap is not None and abs(gap) >= self.GAP_MAG:
            votes["gap"] = 1 if gap > 0 else -1
        else:
            votes["gap"] = 0

        up_count = sum(1 for v in votes.values() if v > 0)
        down_count = sum(1 for v in votes.values() if v < 0)
        total_active = up_count + down_count

        # Disciplined HOLD path.
        if up_count < self.REQUIRED_CONFIRMS and down_count < self.REQUIRED_CONFIRMS:
            miss = []
            for k, v in votes.items():
                if v == 0:
                    miss.append(f"MOMENTUM_{k.upper()}_MISSING")
            reasons = ("MOMENTUM_INSUFFICIENT_CONFIRMS", *miss[:3])
            return StrategyResult(
                action="HOLD", confidence=0.2 + 0.1 * total_active,
                reason_codes=reasons,
                edge_evidence={"votes": dict(votes),
                               "required_confirms": self.REQUIRED_CONFIRMS},
            )

        # Fire on 3+ confirmations. Confidence scales with the
        # number of confirms and the price magnitude.
        confirms = max(up_count, down_count)
        conf = 0.55 + 0.10 * (confirms - self.REQUIRED_CONFIRMS + 1)
        conf = min(0.95, conf + min(0.10, abs(pc) / 10.0))

        if up_count > down_count:
            return StrategyResult(
                action="BUY", confidence=round(conf, 4),
                reason_codes=(
                    "MOMENTUM_UP_CONFIRMED",
                    f"MOMENTUM_CONFIRMS_{confirms}_OF_4",
                    "MOMENTUM_PRICE_SURGE" if abs(pc) >= 1.0 else "MOMENTUM_PRICE_MODERATE",
                ),
                edge_evidence={"votes": dict(votes), "confirms": confirms},
            )
        return StrategyResult(
            action="SELL", confidence=round(conf, 4),
            reason_codes=(
                "MOMENTUM_DOWN_CONFIRMED",
                f"MOMENTUM_CONFIRMS_{confirms}_OF_4",
                "MOMENTUM_PRICE_SURGE" if abs(pc) >= 1.0 else "MOMENTUM_PRICE_MODERATE",
            ),
            edge_evidence={"votes": dict(votes), "confirms": confirms},
        )


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
