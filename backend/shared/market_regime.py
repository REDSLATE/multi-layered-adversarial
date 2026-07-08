"""Market regime resolver — bull / bear / choppy classifier.

Doctrine pin (2026-02-20, Follow-up A from PRD):
    Market regime is a SHARED value, not per-symbol. All 4 brains
    ticking in the same window should see the same value — the
    Governor uses it to modulate risk (RISK_DOWN in choppy tape).

    Read SPY daily bars from `shared_ohlcv_bars` (tf=1d), compute
    a 20-day trend + realized volatility, and classify. Cache
    the result with a short TTL (5 min default) so the 4 brains
    each ticking every 30s don't hammer the same query 40× per
    minute.

Classification (2026-02-20 pin):
    * `bull`   — 20d return ≥ +2% AND normalized realized volatility
                 below the choppy threshold. Trend is up, tape is
                 relatively orderly.
    * `bear`   — 20d return ≤ -2%. Downtrend. Vol threshold does
                 not apply here — a falling market with any vol is
                 already bear.
    * `choppy` — Neither of the above. Sideways / whip-sawing.
                 Governor should RISK_DOWN in this state.
    * `unknown` — Insufficient data (< 20 daily bars available).
                 Consumers should NOT default this to "bull" — leave
                 it as None so the operator sees the gap explicitly.

Env overrides (all optional; defaults documented above):
    MARKET_REGIME_LOOKBACK_DAYS       default 20
    MARKET_REGIME_TREND_THRESHOLD     default 0.02  (2 pct)
    MARKET_REGIME_VOL_CHOPPY_PCT      default 0.015 (1.5 pct daily-log-vol
                                       above this = choppy even if trending up)
    MARKET_REGIME_CACHE_TTL_SEC       default 300 (5 min)
    MARKET_REGIME_BENCHMARK_SYMBOL    default "SPY"
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from typing import Optional

from db import db
from namespaces import SHARED_OHLCV_BARS

logger = logging.getLogger(__name__)


DEFAULT_LOOKBACK_DAYS = 20
DEFAULT_TREND_THRESHOLD = 0.02
DEFAULT_VOL_CHOPPY_PCT = 0.015
DEFAULT_CACHE_TTL_SEC = 300
DEFAULT_BENCHMARK_SYMBOL = "SPY"


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key) or default)
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key) or default)
    except (TypeError, ValueError):
        return default


# Module-level cache: single tuple (regime_dict, computed_at_epoch).
# Since it's the same value for every symbol/brain in a tick window,
# we keep exactly one entry — simpler than a per-key dict.
_CACHE: Optional[tuple[dict, float]] = None


def _now_epoch() -> float:
    return datetime.now(timezone.utc).timestamp()


def _classify(
    trend_20d: float, realized_vol: float,
    trend_threshold: float, vol_choppy: float,
) -> str:
    if trend_20d <= -trend_threshold:
        return "bear"
    if trend_20d >= trend_threshold and realized_vol < vol_choppy:
        return "bull"
    return "choppy"


async def _compute() -> dict:
    """Read SPY daily bars, compute the 20d trend + realized vol,
    return a full regime dict. Missing / short data → regime=None."""
    lookback = _env_int("MARKET_REGIME_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS)
    trend_th = _env_float("MARKET_REGIME_TREND_THRESHOLD", DEFAULT_TREND_THRESHOLD)
    vol_th = _env_float("MARKET_REGIME_VOL_CHOPPY_PCT", DEFAULT_VOL_CHOPPY_PCT)
    symbol = os.environ.get("MARKET_REGIME_BENCHMARK_SYMBOL") or DEFAULT_BENCHMARK_SYMBOL

    bars = await db[SHARED_OHLCV_BARS].find(
        {"symbol": symbol, "tf": "1d"},
        {"_id": 0, "c": 1, "ts": 1},
    ).sort("ts", -1).to_list(lookback)

    if len(bars) < lookback:
        # Insufficient data — regime stays unknown. Doctrine: never
        # default this to "bull". Missing → None; operator sees the
        # gap and can fix upstream (e.g. flatfiles feeder stalled).
        return {
            "regime": None,
            "trend_20d": None,
            "realized_vol": None,
            "bars_seen": len(bars),
            "as_of": datetime.now(timezone.utc).isoformat(),
            "benchmark": symbol,
        }

    # bars are DESCENDING (newest → oldest); flip to ascending.
    closes = [float(b["c"]) for b in reversed(bars)]

    trend_20d = (closes[-1] - closes[0]) / closes[0]

    # Realized volatility: standard deviation of daily log-returns.
    log_rets: list[float] = []
    for prev, curr in zip(closes[:-1], closes[1:]):
        if prev > 0:
            log_rets.append(math.log(curr / prev))
    if not log_rets:
        realized_vol = 0.0
    else:
        mean = sum(log_rets) / len(log_rets)
        var = sum((r - mean) ** 2 for r in log_rets) / len(log_rets)
        realized_vol = math.sqrt(var)

    regime = _classify(trend_20d, realized_vol, trend_th, vol_th)
    return {
        "regime": regime,
        "trend_20d": trend_20d,
        "realized_vol": realized_vol,
        "bars_seen": len(bars),
        "as_of": datetime.now(timezone.utc).isoformat(),
        "benchmark": symbol,
    }


async def get_regime(force_refresh: bool = False) -> dict:
    """TTL-cached regime lookup.

    Returns the cached tuple if computed within the TTL window,
    otherwise recomputes. Thread-safe under asyncio because Mongo
    reads are atomic and a duplicate compute is idempotent (same
    inputs → same output).

    Callers should treat `regime=None` (bars_seen < lookback) as a
    diagnostic — the field simply isn't ready yet.
    """
    global _CACHE
    ttl = _env_int("MARKET_REGIME_CACHE_TTL_SEC", DEFAULT_CACHE_TTL_SEC)
    now = _now_epoch()
    if not force_refresh and _CACHE is not None:
        regime_dict, computed_at = _CACHE
        if (now - computed_at) < ttl:
            return regime_dict
    try:
        computed = await _compute()
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "market_regime._compute failed: %r — returning last cached "
            "value if any, else unknown", e,
        )
        if _CACHE is not None:
            return _CACHE[0]
        return {
            "regime": None,
            "trend_20d": None,
            "realized_vol": None,
            "bars_seen": 0,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "benchmark": DEFAULT_BENCHMARK_SYMBOL,
            "error": str(e)[:200],
        }
    _CACHE = (computed, now)
    return computed


def clear_cache_for_tests() -> None:
    """Test hook — forces the next `get_regime` to recompute."""
    global _CACHE
    _CACHE = None
