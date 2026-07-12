"""P7b replay corpus — synthetic MarketSnapshot builder.

Generates deterministic scenarios covering the full input space
the strategies grade on. Used by the P7c acceptance tests to
prove personality separation is REAL (not just an artefact of
the personality multiplier).

Not a historical bar loader — a real bar loader would introduce
Mongo dependency + slow tests. This module builds snapshots
directly from parameter grids, then the acceptance tests can
assert per-strategy behavior on any scenario deterministically.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType

from mc_pulse.snapshot import MarketSnapshot


def make_snapshot(
    *,
    symbol: str = "TEST",
    lane: str = "equity",
    price: float = 100.0,
    market_regime: str = "trending",
    features: dict | None = None,
    ts: str | None = None,
) -> MarketSnapshot:
    """Build a `MarketSnapshot` with a full feature dict.

    All 4 strategies are pure functions of the feature dict, so
    the acceptance tests can drive them with tiny param variations.
    """
    feats = {
        "symbol": symbol,
        "price": price,
        "market_regime": market_regime,
        # Sensible defaults — override via `features`.
        "trend_score": 0.0,
        "price_change_pct": 0.0,
        "volume_change_pct": 0.0,
        "rsi": 50.0,
        "spread_bps": 5.0,
        "volatility": 0.02,
        "liquidity_score": 0.7,
        "setup_score": 0.5,
        "gap_pct": 0.0,
        "relative_volume": 1.0,
        "vwap_distance_pct": 0.0,
        "spread_quality": "clean",
    }
    if features:
        feats.update(features)

    when = (
        datetime.fromisoformat(ts) if ts
        else datetime(2026, 7, 12, 15, 0, 0, tzinfo=timezone.utc)
    )
    return MarketSnapshot(
        symbol=symbol,
        lane=lane,
        timestamp=when,
        price=Decimal(str(price)),
        indicators=MappingProxyType({}),
        market_state=market_regime,
        feature_snapshot=MappingProxyType(feats),
    )


# ── Canonical scenarios ────────────────────────────────────────
# Named tuples of (label, feature_overrides). Every acceptance
# test iterates these so the replay corpus is a single source
# of truth. Add scenarios here to grow coverage.

REPLAY_SCENARIOS: list[tuple[str, dict]] = [
    # Strong up-trend, clean venue
    ("strong_up_trending", {
        "trend_score": 0.75, "price_change_pct": 0.60,
        "volume_change_pct": 0.80, "relative_volume": 1.8,
        "rsi": 60.0, "vwap_distance_pct": 1.0,
        "market_regime": "trending",
    }),
    # Overbought (RSI extreme) — mean reversion territory
    ("overbought_extreme", {
        "trend_score": 0.30, "price_change_pct": 0.20,
        "rsi": 82.0, "vwap_distance_pct": 3.5,
        "market_regime": "trending",
    }),
    # Oversold (RSI extreme)
    ("oversold_extreme", {
        "trend_score": -0.30, "price_change_pct": -0.20,
        "rsi": 22.0, "vwap_distance_pct": -3.5,
        "market_regime": "trending",
    }),
    # Wide-spread event — execution veto
    ("wide_spread_hostile_venue", {
        "trend_score": 0.60, "price_change_pct": 0.40,
        "spread_bps": 45.0, "liquidity_score": 0.6,
        "market_regime": "trending",
    }),
    # Vol spike (news-driven)
    ("volatility_spike_news", {
        "trend_score": 0.40, "price_change_pct": 0.30,
        "volatility": 0.12, "relative_volume": 2.5,
        "market_regime": "vol_expansion",
    }),
    # Low liquidity crypto
    ("low_liquidity_crypto", {
        "trend_score": 0.20, "price_change_pct": 0.15,
        "liquidity_score": 0.20, "spread_bps": 35.0,
        "market_regime": "ranging",
    }),
    # Ranging market — no signal
    ("ranging_quiet", {
        "trend_score": 0.05, "price_change_pct": 0.02,
        "rsi": 51.0, "market_regime": "ranging",
    }),
    # Gap up + volume — momentum's ideal setup
    ("gap_up_with_volume", {
        "trend_score": 0.55, "price_change_pct": 0.80,
        "volume_change_pct": 1.20, "relative_volume": 2.5,
        "gap_pct": 1.20, "market_regime": "trending",
    }),
    # Strong down-trend
    ("strong_down_trending", {
        "trend_score": -0.75, "price_change_pct": -0.60,
        "volume_change_pct": 0.80, "relative_volume": 1.8,
        "rsi": 38.0, "vwap_distance_pct": -1.0,
        "market_regime": "trending",
    }),
    # Trend + overbought — Barracuda vs the rest
    ("uptrend_meeting_overbought", {
        "trend_score": 0.65, "price_change_pct": 0.30,
        "rsi": 76.0, "vwap_distance_pct": 3.2,
        "market_regime": "trending",
    }),
]
