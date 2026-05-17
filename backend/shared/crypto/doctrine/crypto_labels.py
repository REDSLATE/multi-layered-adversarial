"""Crypto-only doctrine labeler.

Doctrine (2026-02-17): twin of `shared.doctrine.base_labels` but with
crypto-native features only.

    SAFE:    24h volume, spread_bps, vol_1h, trend strength,
             funding rate, OI change, liquidation imbalance,
             BTC regime alignment, exchange liquidity.

    BANNED: gap_pct, float_millions, premarket gap, small-cap price
            filters — those are equity-only and have no crypto meaning.

Fails closed on `lane != "crypto"` (returns REJECT with WRONG_LANE).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


CRYPTO_DOCTRINE_VERSION = "crypto_sidecar_v1"


@dataclass(frozen=True)
class CryptoDoctrineLabels:
    lane: str
    symbol: str
    score: float
    quality: str
    labels: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    doctrine_version: str = CRYPTO_DOCTRINE_VERSION


def _num(snapshot: Dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        value = snapshot.get(key, default)
        if value is None:
            return default
        return float(value)
    except Exception:  # noqa: BLE001
        return default


def _quality(score: float) -> str:
    if score >= 0.80:
        return "A_QUALITY"
    if score >= 0.60:
        return "B_QUALITY"
    if score >= 0.40:
        return "C_QUALITY"
    return "REJECT"


def label_crypto_snapshot(snapshot: Dict[str, Any]) -> CryptoDoctrineLabels:
    """Crypto-only labeler. Equity-flavored callers are rejected loudly."""

    symbol = str(snapshot.get("symbol", "UNKNOWN"))
    lane = str(snapshot.get("lane", "crypto")).lower()

    if lane != "crypto":
        return CryptoDoctrineLabels(
            lane=lane,
            symbol=symbol,
            score=0.0,
            quality="REJECT",
            labels=["WRONG_LANE"],
            reasons=["crypto doctrine received non-crypto lane snapshot"],
        )

    score = 0.0
    labels: List[str] = []
    reasons: List[str] = []

    volume_24h_usd = _num(snapshot, "volume_24h_usd")
    spread_bps = _num(snapshot, "spread_bps", 9999.0)
    volatility_1h = _num(snapshot, "volatility_1h")
    trend_strength = _num(snapshot, "trend_strength")
    funding_rate = _num(snapshot, "funding_rate")
    open_interest_change_pct = _num(snapshot, "open_interest_change_pct")
    liquidation_imbalance = _num(snapshot, "liquidation_imbalance")
    btc_regime_alignment = _num(snapshot, "btc_regime_alignment")
    exchange_liquidity_score = _num(snapshot, "exchange_liquidity_score")

    if volume_24h_usd >= 50_000_000:
        score += 0.15
        labels.append("HIGH_24H_VOLUME")
        reasons.append("24h volume supports executable liquidity")

    if spread_bps <= 25:
        score += 0.15
        labels.append("TIGHT_SPREAD")
        reasons.append("spread is tight enough for small-account execution")
    elif spread_bps > 75:
        score -= 0.15
        labels.append("WIDE_SPREAD")
        reasons.append("spread is too wide for clean execution")

    if exchange_liquidity_score >= 0.70:
        score += 0.15
        labels.append("EXCHANGE_LIQUIDITY_OK")
        reasons.append("exchange liquidity score is acceptable")

    if trend_strength >= 0.65:
        score += 0.15
        labels.append("TREND_ALIGNED")
        reasons.append("trend strength supports directional continuation")

    if volatility_1h >= 0.015:
        score += 0.10
        labels.append("VOL_EXPANSION")
        reasons.append("1h volatility expansion is present")
    elif volatility_1h <= 0.003:
        score -= 0.10
        labels.append("DEAD_VOL")
        reasons.append("volatility is too compressed")

    if abs(funding_rate) <= 0.0005:
        score += 0.10
        labels.append("FUNDING_NEUTRAL")
        reasons.append("funding is not extremely crowded")
    else:
        score -= 0.10
        labels.append("FUNDING_CROWDED")
        reasons.append("funding suggests crowding risk")

    if open_interest_change_pct >= 3:
        score += 0.10
        labels.append("OI_EXPANSION")
        reasons.append("open interest expansion confirms participation")

    if abs(liquidation_imbalance) <= 0.50:
        score += 0.10
        labels.append("LIQUIDATION_BALANCED")
        reasons.append("liquidation imbalance is not extreme")
    else:
        score -= 0.10
        labels.append("LIQUIDATION_RISK")
        reasons.append("liquidation imbalance is extreme")

    if btc_regime_alignment >= 0.60:
        score += 0.10
        labels.append("BTC_REGIME_ALIGNED")
        reasons.append("asset direction aligns with BTC regime")

    score = max(0.0, min(1.0, score))

    return CryptoDoctrineLabels(
        lane="crypto",
        symbol=symbol,
        score=round(score, 4),
        quality=_quality(score),
        labels=labels,
        reasons=reasons,
    )
