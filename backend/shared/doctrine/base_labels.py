"""Shared doctrine core — lane-neutral setup-quality labeler.

Doctrine (2026-02-17):
    This module does NOT decide BUY/SELL/HOLD.
    This module does NOT execute.
    This module only LABELS setup quality from market facts.

    The four brains consume these labels through their own
    `doctrine_interpreter.py` modules to produce role-flavored
    sidecars (Alpha = strategist, REDEYE = adversary, Chevelle =
    governor, Camaro = execution judge).

    Output is a `DoctrineLabels` packet that any intent / mission can
    carry. Shelly can persist it for later verified learning.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List


@dataclass
class DoctrineLabels:
    symbol: str
    score: float
    quality: str
    labels: List[str]
    reasons: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def build_doctrine_labels(snapshot: Dict[str, Any]) -> DoctrineLabels:
    """Shared doctrine core. No decisions, no execution, just labels."""

    symbol = str(snapshot.get("symbol", "UNKNOWN"))

    price = float(snapshot.get("price", 0.0))
    gap_pct = float(snapshot.get("gap_pct", 0.0))
    relative_volume = float(snapshot.get("relative_volume", 0.0))
    has_news = bool(snapshot.get("has_news", False))
    float_millions = float(snapshot.get("float_millions", 999999.0))
    pattern = str(snapshot.get("pattern", "")).lower()
    market_regime = str(snapshot.get("market_regime", "unknown")).lower()
    spread_bps = float(snapshot.get("spread_bps", 999.0))

    score = 0.0
    labels: List[str] = []
    reasons: List[str] = []

    if 1.00 <= price <= 20.00:
        score += 0.15
        labels.append("SMALL_ACCOUNT_PRICE_VALID")
    else:
        reasons.append("price_outside_1_to_20")

    if gap_pct >= 10.0:
        score += 0.15
        labels.append("GAPPER")
    else:
        reasons.append("gap_below_10_pct")

    if relative_volume >= 5.0:
        score += 0.20
        labels.append("HIGH_RELATIVE_VOLUME")
    else:
        reasons.append("relative_volume_below_5x")

    if has_news:
        score += 0.15
        labels.append("NEWS_CATALYST")
    else:
        labels.append("NO_NEWS_RISK")
        reasons.append("no_news_catalyst")

    if float_millions <= 20.0:
        score += 0.15
        labels.append("LOW_FLOAT_SUPPLY_IMBALANCE")
    else:
        reasons.append("float_above_20m")

    if pattern in {"pullback", "dip", "first_pullback", "micro_pullback"}:
        score += 0.10
        labels.append("VALID_PULLBACK_PATTERN")
    else:
        reasons.append("no_valid_pullback_pattern")

    if market_regime in {"strong", "green_light", "momentum"}:
        score += 0.10
        labels.append("MARKET_GREEN_LIGHT")
    elif market_regime in {"weak", "slow", "choppy"}:
        score -= 0.15
        labels.append("MARKET_WEAK_REDUCE_RISK")
        reasons.append("weak_market_regime")

    if spread_bps <= 75.0:
        labels.append("SPREAD_ACCEPTABLE")
    else:
        score -= 0.15
        labels.append("SPREAD_TOO_WIDE")
        reasons.append("spread_too_wide")

    score = _clamp(score)

    if score >= 0.80:
        quality = "A_QUALITY"
    elif score >= 0.60:
        quality = "B_QUALITY"
    elif score >= 0.40:
        quality = "C_QUALITY"
    else:
        quality = "REJECT"

    return DoctrineLabels(
        symbol=symbol,
        score=score,
        quality=quality,
        labels=labels,
        reasons=reasons,
    )
