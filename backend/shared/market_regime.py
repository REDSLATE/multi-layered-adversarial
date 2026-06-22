"""Market regime classifier — composite of breadth, vol, and trend.

Doctrine pin (operator directive, 2026-06-10, P1):
Before this module, every brain snapshot carried `market_regime="calm"`
hardcoded. That made `legacy_brain_wrappers.apply_camaro_legacy_doctrine`
mis-attribute every market state as calm — the chop-detection logic in
particular never engaged. With this module the regime is derived from
the brain runner's own universe scan: the same symbols the brain is
about to evaluate carry the signal MC uses to set the market backdrop.

Regime taxonomy (matches `legacy_brain_wrappers` consumers):
    calm     — trend mild, vol low, breadth balanced
    bull     — trend positive, vol low/mid, breadth wide-up
    bear     — trend negative, vol low/mid, breadth wide-down
    chop     — trend ≈ 0, breadth ≈ 0, vol low — symmetric noise
    volatile — vol high, trend any
    crisis   — vol extreme

Inputs (all already computed by the runner's per-symbol scan):
    * mean_trend_score in [-1, 1]
    * mean_volatility  in [0, 1]
    * breadth          in [-1, 1]  (advancers - decliners) / total

Doctrine constraints:
    * Pure function. No I/O. Trivially unit-testable.
    * Returns a stable string identifier consumed by downstream
      wrappers. Adding a new regime requires updating the wrapper
      registry too.
    * Thresholds are operator-tunable here, not via env. Regime
      semantics should be code-reviewed, not config-toggled.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


# Thresholds — operator-pinned 2026-06-10. Re-tune by editing here.
_VOL_CRISIS = 0.70   # ≥ → crisis regardless of trend/breadth
_VOL_VOLATILE = 0.45  # ≥ but < crisis → volatile
_TREND_DIRECTIONAL = 0.20  # |mean_trend| ≥ this → directional regime
_BREADTH_DIRECTIONAL = 0.20  # |breadth| ≥ this → confirms direction
_CHOP_TIGHT_BAND = 0.10  # both trend and breadth inside this → chop


@dataclass(frozen=True)
class RegimeSignal:
    """Diagnostic envelope around the regime decision.

    Carries the inputs alongside the verdict so the wrapper layer
    (and any audit code) can show the operator WHY a regime was
    chosen — not just WHAT it was.
    """
    regime: str
    mean_trend_score: float
    mean_volatility: float
    breadth: float
    sample_size: int


def classify_market_regime(
    *,
    mean_trend_score: float,
    mean_volatility: float,
    breadth: float,
) -> str:
    """Pure classifier. Returns one of:
    {calm, bull, bear, chop, volatile, crisis}.

    Decision order (highest priority first):
        1. Vol extreme  → crisis
        2. Vol elevated → volatile
        3. Trend + breadth both directional + agreeing → bull/bear
        4. Trend + breadth both flat → chop
        5. Everything else (mixed signals)             → calm
    """
    t = float(mean_trend_score or 0.0)
    v = float(mean_volatility or 0.0)
    b = float(breadth or 0.0)

    # Crisis trumps everything — even a "bull" tape in extreme vol
    # is still crisis-mode for risk purposes.
    if v >= _VOL_CRISIS:
        return "crisis"
    if v >= _VOL_VOLATILE:
        return "volatile"

    # Directional regimes — require BOTH trend and breadth to agree.
    if t >= _TREND_DIRECTIONAL and b >= _BREADTH_DIRECTIONAL:
        return "bull"
    if t <= -_TREND_DIRECTIONAL and b <= -_BREADTH_DIRECTIONAL:
        return "bear"

    # Chop — symmetric noise. Tight band around zero on BOTH axes.
    if abs(t) <= _CHOP_TIGHT_BAND and abs(b) <= _CHOP_TIGHT_BAND:
        return "chop"

    # Anything left = mixed signals → calm. Examples:
    #   * trend positive but breadth negative (rotation, not bull)
    #   * trend flat but breadth strong (early move; not committed)
    return "calm"


def classify_from_symbol_snapshots(
    snapshots: Iterable[dict],
) -> RegimeSignal:
    """Convenience wrapper — derive the three composite inputs from
    a sequence of per-symbol snapshots already produced by the
    runner.

    Each snapshot is expected to carry:
        * `trend_score`      in [-1, 1]
        * `volatility`       in [0, 1]
        * `price_change_pct` (for breadth — sign-only contribution)

    Robust to missing fields — defaults to 0 and uses len() for
    sample_size. Returns a `RegimeSignal` with both verdict and
    inputs for downstream audit.
    """
    trends: list[float] = []
    vols: list[float] = []
    pcs: list[float] = []
    for s in snapshots:
        try:
            trends.append(float(s.get("trend_score") or 0.0))
        except (TypeError, ValueError):
            pass
        try:
            vols.append(float(s.get("volatility") or 0.0))
        except (TypeError, ValueError):
            pass
        try:
            pcs.append(float(s.get("price_change_pct") or 0.0))
        except (TypeError, ValueError):
            pass

    n = len(trends) or 1
    mean_t = sum(trends) / n if trends else 0.0
    mean_v = sum(vols) / (len(vols) or 1) if vols else 0.0
    # Breadth = (advancers - decliners) / total. Symbols with exactly
    # 0 price_change are treated as neutral (don't count either way).
    ups = sum(1 for p in pcs if p > 0)
    downs = sum(1 for p in pcs if p < 0)
    total = ups + downs
    breadth = ((ups - downs) / total) if total else 0.0

    regime = classify_market_regime(
        mean_trend_score=mean_t,
        mean_volatility=mean_v,
        breadth=breadth,
    )
    return RegimeSignal(
        regime=regime,
        mean_trend_score=mean_t,
        mean_volatility=mean_v,
        breadth=breadth,
        sample_size=len(trends),
    )


__all__ = [
    "RegimeSignal",
    "classify_market_regime",
    "classify_from_symbol_snapshots",
]
