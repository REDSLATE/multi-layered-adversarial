"""Adapter: build a SqueezeInput from the doctrine snapshot + Webull bars.

Operator shipped `squeeze_detector_v2.py` 2026-06-11. The detector is
brain-input-agnostic — this adapter converts the live equity snapshot
(already enriched with Webull data) into the SqueezeInput shape the
detector expects, then runs the analysis and returns a JSON-ready
block.

What we map directly from the snapshot:
    price, prev_close (snapshot.pre_close), day_high (max bar high),
    volume_today, avg_volume_20d (volume_today / relative_volume),
    spread_bps, data_freshness_ms

What we derive from Webull M1 bars:
    price_30s_ago (close of last-but-one bar — approx),
    volume_last_1m (last bar volume),
    avg_volume_last_5m (avg volume of last 5 bars),
    premarket_high (max high of bars; approximation — full premarket
        timestamp filtering is a future enhancement),
    news_catalyst (we pass False until a news source is wired)

What we cannot populate today (left as None — detector applies
`data_incomplete_risk`):
    float_shares          (no Webull source under current entitlements)
    short_interest_pct    (no Webull source)
    borrow_rate_pct       (no Webull source)
    borrow_rate_change_pct (no Webull source)
    shares_available_to_short (no Webull source)

These can be plumbed in from a future Finnhub/Polygon side-fetch
without changing the detector contract.
"""
from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from shared.squeeze.squeeze_detector_v2 import (
    SqueezeDetectorV2,
    SqueezeInput,
    SqueezeResult,
)

_DETECTOR = SqueezeDetectorV2()


def _to_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _bar_close(b: Dict[str, Any]) -> float:
    return float(b.get("close") or b.get("c") or 0.0)


def _bar_volume(b: Dict[str, Any]) -> float:
    return float(b.get("volume") or b.get("v") or 0.0)


def _bar_high(b: Dict[str, Any]) -> float:
    return float(b.get("high") or b.get("h") or 0.0)


def build_squeeze_block(
    symbol: str,
    snapshot: Dict[str, Any],
    bars: Optional[List[Dict[str, Any]]] = None,
    *,
    data_freshness_ms: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Run the squeeze detector against the live equity snapshot.

    Returns a JSON-ready dict matching `SqueezeResult` or `None` when
    the inputs are too incomplete to even attempt analysis (no price /
    no prev_close — those would just yield a `DATA_ERROR` grade which
    would clutter every cold-start brain tick).
    """
    sym = (symbol or "").upper()
    price = _to_float(snapshot.get("price"))
    prev_close = _to_float(snapshot.get("pre_close")) or _to_float(snapshot.get("prev_close"))
    if not price or not prev_close or price <= 0 or prev_close <= 0:
        return None

    bars = bars or []

    # day_high: from snapshot if present, else max bar high
    day_high = _to_float(snapshot.get("high"))
    if (not day_high or day_high <= 0) and bars:
        day_high = max((_bar_high(b) for b in bars), default=0.0)
    if not day_high or day_high <= 0:
        # Fall back to current price so the detector doesn't fail the
        # hard-field validation. Result: `already_fading_from_high` can
        # never fire on this tick, which is the correct degraded mode.
        day_high = price

    volume_today = _to_float(snapshot.get("volume")) or 0.0
    rel_vol = _to_float(snapshot.get("relative_volume")) or 0.0
    # avg_volume_20d: invert relative_volume = today/avg => avg = today/rel_vol
    if rel_vol > 0 and volume_today > 0:
        avg_volume_20d = volume_today / rel_vol
    else:
        # Without RVOL we can't reconstruct avg_volume_20d. Use 1.0 ×
        # today as a neutral baseline so the detector still runs without
        # rewarding (rel_volume = 1.0).
        avg_volume_20d = volume_today if volume_today > 0 else 1.0

    # Bar-derived velocity & volume signals
    price_30s_ago = _bar_close(bars[-2]) if len(bars) >= 2 else None
    if price_30s_ago is not None and price_30s_ago <= 0:
        price_30s_ago = None
    volume_last_1m = _bar_volume(bars[-1]) if bars else None
    if volume_last_1m is not None and volume_last_1m <= 0:
        volume_last_1m = None
    if len(bars) >= 5:
        last_5_vols = [_bar_volume(b) for b in bars[-5:]]
        avg_volume_last_5m = sum(last_5_vols) / 5.0 if all(v > 0 for v in last_5_vols) else None
    else:
        avg_volume_last_5m = None

    premarket_high = max((_bar_high(b) for b in bars), default=None) if bars else None
    if premarket_high is not None and premarket_high <= 0:
        premarket_high = None

    spread_bps = _to_float(snapshot.get("spread_bps"))

    inp = SqueezeInput(
        symbol=sym,
        price=price,
        prev_close=prev_close,
        day_high=day_high,
        premarket_high=premarket_high,
        volume_today=volume_today,
        avg_volume_20d=avg_volume_20d,
        float_shares=None,
        timestamp=time.time(),
        data_freshness_ms=data_freshness_ms,
        short_interest_pct=None,
        borrow_rate_pct=None,
        borrow_rate_change_pct=None,
        shares_available_to_short=None,
        spread_bps=spread_bps,
        news_catalyst=bool(snapshot.get("has_news", False)),
        price_30s_ago=price_30s_ago,
        volume_last_1m=volume_last_1m,
        avg_volume_last_5m=avg_volume_last_5m,
    )
    result = _DETECTOR.analyze(inp)
    return asdict(result)


def analyze(inp: SqueezeInput) -> SqueezeResult:
    """Pass-through to the detector — used by direct callers / tests."""
    return _DETECTOR.analyze(inp)
