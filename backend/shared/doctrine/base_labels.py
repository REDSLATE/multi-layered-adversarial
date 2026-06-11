"""Shared doctrine core — lane-neutral setup-quality labeler.

Doctrine (2026-02-17, rev2 — source-aligned):
    This module does NOT decide BUY/SELL/HOLD.
    This module does NOT execute.
    This module only LABELS setup quality from market facts.

    Source material: 2025 Small Account Tool Kit + Technical Analysis
    Gap-and-Go / Micro-Pullback (Warrior Trading). Numeric thresholds
    here are pinned to that material so doctrine matches the spec it
    claims to encode.

    Tier upgrades — when a fact CLEARS A TIGHTER THRESHOLD than the
    minimum, an upgraded label is emitted with a small additive score
    bonus. Existing A_QUALITY cases stay A_QUALITY because the total
    clamps at 1.0; tier upgrades just put more daylight between A and
    B/C setups so the Patent J ladder has signal to grade against.

    The four brains consume these labels through their own
    `doctrine_interpreter.py` modules to produce role-flavored
    sidecars. Output is a `DoctrineLabels` packet that any intent /
    mission can carry. Shelly persists it for verified learning.
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


# Source: 2025 Small Account Tool Kit pp. 1-3; Technical Analysis v3
# (Gap-and-Go + Micro Pullback). All thresholds traceable to those
# documents — DO NOT drift without bumping doctrine_version.
VALID_PULLBACK_PATTERNS = {
    # generic pullback family (Toolkit)
    "pullback", "dip", "first_pullback", "micro_pullback",
    # specific named patterns (Tech Analysis v3)
    "bull_flag", "flat_top_breakout",
}


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
    # New: time-of-day filter (Toolkit: 7am-11am EST trading window).
    # Snapshot may pass `hour_et` (0-23) when available; absence is
    # treated as informational, not penalized.
    hour_et = snapshot.get("hour_et")

    score = 0.0
    labels: List[str] = []
    reasons: List[str] = []

    # ── price band ──────────────────────────────────────────────────
    if 1.00 <= price <= 20.00:
        score += 0.15
        labels.append("SMALL_ACCOUNT_PRICE_VALID")
        # Tier upgrade: $5-$10 is the documented sweet spot.
        if 5.00 <= price <= 10.00:
            score += 0.03
            labels.append("SWEET_SPOT_PRICE")
    else:
        reasons.append("price_outside_1_to_20")

    # ── gap band ───────────────────────────────────────────────────
    if gap_pct >= 10.0:
        score += 0.15
        labels.append("GAPPER")
        # Tier upgrade: Tech Analysis v3 prefers ≥20% gap.
        if gap_pct >= 20.0:
            score += 0.03
            labels.append("STRONG_GAPPER")
    else:
        reasons.append("gap_below_10_pct")

    # ── relative volume ────────────────────────────────────────────
    if relative_volume >= 5.0:
        score += 0.20
        labels.append("HIGH_RELATIVE_VOLUME")
    else:
        reasons.append("relative_volume_below_5x")

    # ── news catalyst ──────────────────────────────────────────────
    if has_news:
        score += 0.15
        labels.append("NEWS_CATALYST")
    else:
        labels.append("NO_NEWS_RISK")
        reasons.append("no_news_catalyst")

    # ── float / supply imbalance ───────────────────────────────────
    if float_millions <= 20.0:
        score += 0.15
        labels.append("LOW_FLOAT_SUPPLY_IMBALANCE")
        # Tier upgrade: <10M is "cold market" threshold per Toolkit.
        if float_millions < 10.0:
            score += 0.03
            labels.append("ULTRA_LOW_FLOAT")
    else:
        reasons.append("float_above_20m")

    # ── pattern ────────────────────────────────────────────────────
    # SAC2024 refinement: pullback patterns are only valid on stocks
    # that are ALREADY leading on attention — i.e., a gapper OR has
    # high relative volume. A "pullback" on a stale, low-RVOL ticker
    # doesn't earn the label.
    has_leading_attention = (
        gap_pct >= 10.0 or relative_volume >= 5.0
    )
    if pattern in VALID_PULLBACK_PATTERNS and has_leading_attention:
        score += 0.10
        labels.append("VALID_PULLBACK_PATTERN")
        # Surface the specific named pattern when present so the
        # strategy split (Phase C) can branch on it.
        if pattern == "bull_flag":
            labels.append("BULL_FLAG_PATTERN")
        elif pattern == "flat_top_breakout":
            labels.append("FLAT_TOP_BREAKOUT_PATTERN")
        elif pattern in {"micro_pullback", "first_pullback"}:
            labels.append("MICRO_PULLBACK_PATTERN")
    elif pattern in VALID_PULLBACK_PATTERNS and not has_leading_attention:
        # Pattern present, but the stock isn't leading — SAC2024 says
        # this is the trap. Don't score it; surface a reason.
        labels.append("PULLBACK_PATTERN_ON_NON_LEADER")
        reasons.append("pullback_on_non_leading_stock")
    else:
        reasons.append("no_valid_pullback_pattern")

    # ── market regime ──────────────────────────────────────────────
    if market_regime in {"strong", "green_light", "momentum"}:
        score += 0.10
        labels.append("MARKET_GREEN_LIGHT")
    elif market_regime in {"weak", "slow", "choppy"}:
        score -= 0.15
        labels.append("MARKET_WEAK_REDUCE_RISK")
        reasons.append("weak_market_regime")

    # ── parabolic-phase adaptive sizing (2026-06-11 operator directive) ──
    # Reads the parabolic_phase classifier output. We intentionally do NOT
    # add a hard block on any phase — sizing scales continuously via
    # score deltas so the brain still ships intents on parabolic moves,
    # just at smaller size with tighter stops. Quality over quantity.
    parabolic_phase = str(snapshot.get("parabolic_phase", "")).lower()
    velocity_5m = float(snapshot.get("velocity_5m", 0.0))
    if parabolic_phase == "accumulation":
        score += 0.05
        labels.append("ACCUMULATION_HEALTHY_EXPANSION")
    elif parabolic_phase == "parabolic":
        # Continuous scale-down: at +8% velocity → -0.10, at +20% → -0.30
        # Linear clamp keeps the brain emitting but at progressively
        # smaller size as the move extends past the fade-risk threshold.
        v = max(0.0, velocity_5m - 8.0)  # excess above threshold
        penalty = min(0.30, 0.10 + (v / 12.0) * 0.20)
        score -= penalty
        labels.append("PARABOLIC_LATE_ENTRY_RISK")
        reasons.append(f"parabolic_5m_velocity_{velocity_5m:.1f}pct")
    elif parabolic_phase == "topping":
        # Two-red-bar confirmation already happened in the classifier.
        # Score is hit hard — brains drop BUY confidence, raise SELL.
        score -= 0.25
        labels.append("TOPPING_DISTRIBUTION_STARTED")
        reasons.append("topping_two_red_after_run")
    elif parabolic_phase == "fade":
        score -= 0.25
        labels.append("FADE_LOWER_HIGHS_LOWER_LOWS")
        reasons.append("fade_off_session_peak")

    # ── spread / liquidity ─────────────────────────────────────────
    if spread_bps <= 75.0:
        labels.append("SPREAD_ACCEPTABLE")
    else:
        score -= 0.15
        labels.append("SPREAD_TOO_WIDE")
        reasons.append("spread_too_wide")

    # ── time-of-day window (informational; no score bonus, only label)
    # Toolkit pp.3-4: prime trading window 7-11am EST. Outside-window
    # trades aren't blocked but surface the label so the operator can
    # filter them later in the scorecard.
    if isinstance(hour_et, (int, float)):
        if 7 <= int(hour_et) < 11:
            labels.append("TRADING_WINDOW_PRIME")
        else:
            labels.append("TRADING_WINDOW_OFF_HOURS")

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
