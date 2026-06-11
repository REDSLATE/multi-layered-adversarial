"""Equity doctrine enricher — populates strategy-doctrine fields.

Operator directive (2026-06-11):
    All 5 doctrines were stuck at LEARNING 0/100 because the equity
    brains never received the fields the strategy doctrines need
    (`gap_pct`, `relative_volume`, `market_cap_band`, etc.). The
    brain runner's `_build_snapshot()` produces a generic trend /
    volatility / pattern snapshot but does NOT populate the doctrine-
    facing fields. This module bridges that gap.

How it plugs in:
    `external/brains/runner.py::_evaluate_and_post` calls
    `enrich_equity_doctrine_snapshot(symbol, base_snapshot)` after
    `_build_snapshot()` and before the brain decides. The enricher
    returns a NEW dict — `base_snapshot` is never mutated in place
    so the cold-start regime classifier still sees the original.

Data sources (all Webull, all free under the operator's active
"Nasdaq Basic - Non Display" Open API entitlement, 2027-06-10):
    * `market_data.get_snapshot(["AAPL"], "US_STOCK")` →
        price, bid/ask (→ spread_bps), pre_close (→ gap_pct),
        open, high, low, volume, change_ratio
    * `screener.get_most_active(...)` → relative_volume_10d
        (only populated when the ticker is on the hot list)
    * `instrument.get_instrument(["AAPL"])` → marginable /
        shortable / fractionable / overnight_trading_supported
    * `market_data.get_history_bar("AAPL", "M1", count=30)` →
        recent bars for momentum / pullback / pattern hints

Fields populated for the doctrine:
    gap_pct, relative_volume, spread_bps, market_cap_band,
    near_half_or_whole_dollar, momentum_active, pullback_low,
    no_nearby_resistance, price_above_emas, pattern, hour_et

NOT populated (left to other sources):
    has_news, float_millions — Webull doesn't expose these on the
    paths we have entitlement for. Doctrine treats absence as
    "no news" / "high float" so missing values are non-penalizing
    only in the directions we want (gap-and-go won't fire without
    a real catalyst flag, which is correct fail-closed behavior).

Fail-soft:
    Any exception or missing data returns the base snapshot
    unchanged. Brains keep working — they just emit on the lean
    snapshot that day. No exceptions propagate to the runner.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("risedual.snapshot_enrich.equity")

# Mega/large-cap tickers — these flip the lane router to
# `large_cap_equity_v1`. Pinned roster (not exhaustive, just the
# universe we trade today). Anything not listed defaults to "small".
_MEGA_CAP_SYMBOLS = {
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA",
    "BRK.A", "BRK.B", "LLY", "AVGO", "JPM", "V", "WMT", "XOM", "JNJ",
    "MA", "PG", "ORCL", "HD", "BAC", "ABBV", "COST", "NFLX", "KO",
    "ADBE", "CRM", "CSCO", "AMD", "MCD", "PEP", "TMO", "ABT", "QCOM",
    "BABA", "TSM", "ASML", "AXP", "DIS", "INTC", "IBM", "BA",
}


def _to_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def _market_cap_band(symbol: str, instr: Optional[Dict[str, Any]]) -> str:
    """Return `'mega' | 'large' | 'mid' | 'small'`.

    Webull's instrument endpoint does NOT return market cap directly.
    The `etf_leveraged_flag` field is populated for ALL US equities
    (not just ETFs) so we can't use it as a "large" signal. Until we
    wire a real cap source (Polygon ticker details has `market_cap`),
    we use the pinned mega-cap roster and default everything else to
    "small". This correctly routes our universe of $2-$25 day-trading
    candidates (AAL, AMC, AEO, etc.) into the small-account doctrine.
    """
    sym = (symbol or "").upper()
    if sym in _MEGA_CAP_SYMBOLS:
        return "mega"
    return "small"


def _near_half_or_whole_dollar(price: float, tolerance: float = 0.05) -> bool:
    """True when price is within `tolerance` of an $X.00 or $X.50 level.

    Micro-pullback doctrine source: entries on the 1-min pullback to
    a half/whole-dollar magnet. Used as a boolean for the doctrine
    sidecar, not a sizing input.
    """
    if price <= 0:
        return False
    frac = price - int(price)
    return frac < tolerance or abs(frac - 0.5) < tolerance or frac > (1.0 - tolerance)


def _momentum_active(bars: List[Dict[str, Any]]) -> bool:
    """True when the last 5 M1 bars are net-positive AND price is
    above the M1 mean. Lightweight heuristic that won't lie when bars
    are sparse — fewer than 5 bars returns False (fail-closed)."""
    if len(bars) < 5:
        return False
    closes = [_to_float(b.get("close") or b.get("c")) for b in bars[-5:]]
    if not all(closes) or any(c <= 0 for c in closes):
        return False
    net = closes[-1] - closes[0]
    avg = sum(closes) / len(closes)
    return net > 0 and closes[-1] > avg


def _pullback_low(bars: List[Dict[str, Any]]) -> Optional[float]:
    """Lowest low across the most recent 10 M1 bars after the peak.

    Returns `None` if we can't identify a pullback structure.
    Doctrine requires this as the stop reference — if we can't find
    it, the micro-pullback strategy refuses to fire (correct).
    """
    if len(bars) < 10:
        return None
    tail = bars[-10:]
    highs = [_to_float(b.get("high") or b.get("h")) for b in tail]
    lows = [_to_float(b.get("low") or b.get("l")) for b in tail]
    if not all(highs) or not all(lows):
        return None
    peak_idx = highs.index(max(highs))
    # Need at least 2 bars after the peak to call it a pullback
    if peak_idx >= len(tail) - 2:
        return None
    return min(lows[peak_idx:])


def _no_nearby_resistance(bars: List[Dict[str, Any]], current: float) -> bool:
    """True when the last 30 M1 bars have NO high within 0.5% above
    `current`. Doctrine treats "clean air to next .50/$1 magnet" as
    a green light for the 50¢ target."""
    if not bars or current <= 0:
        return False
    highs = [_to_float(b.get("high") or b.get("h")) for b in bars[-30:]]
    if not highs:
        return False
    band_top = current * 1.005
    nearby = [h for h in highs if current < h <= band_top]
    return len(nearby) == 0


def _detect_pattern(bars: List[Dict[str, Any]]) -> Optional[str]:
    """Coarse pattern detector for the Tech Analysis v3 setups.

    Returns one of: `"micro_pullback"`, `"bull_flag"`,
    `"flat_top_breakout"`, `"pullback"`, or `None`.

    Heuristics — intentionally simple. The doctrine score adjusts the
    final intent; this is just to give the brain a pattern label to
    branch on.
    """
    if len(bars) < 10:
        return None
    tail = bars[-10:]
    closes = [_to_float(b.get("close") or b.get("c")) for b in tail]
    highs = [_to_float(b.get("high") or b.get("h")) for b in tail]
    lows = [_to_float(b.get("low") or b.get("l")) for b in tail]
    if not all(closes):
        return None
    # Detect: rising leg then 2-4 consolidation bars at the top
    open_close_change = (closes[-1] - closes[0]) / (closes[0] or 1.0)
    if open_close_change > 0.005:  # net up >0.5%
        peak_idx = highs.index(max(highs))
        # After-peak range tightness — bull flag has a tight pullback
        if peak_idx < len(tail) - 2:
            after = tail[peak_idx + 1:]
            after_highs = [_to_float(b.get("high") or b.get("h")) for b in after]
            after_lows = [_to_float(b.get("low") or b.get("l")) for b in after]
            if after_highs and after_lows:
                range_pct = (max(after_highs) - min(after_lows)) / (closes[peak_idx] or 1.0)
                if range_pct < 0.005:
                    return "flat_top_breakout"
                if range_pct < 0.015:
                    return "bull_flag"
                if range_pct < 0.03:
                    return "micro_pullback"
        return "pullback"
    return None


def _hour_et() -> int:
    """Hours since midnight ET (24h). Used by the toolkit's prime-window
    label. Approximation via UTC offset is acceptable — DST drift only
    affects the 7-11am window label by 1 hour twice a year, doctrine
    is informational not gating."""
    # ET is UTC-5 (EST) or UTC-4 (EDT). Use UTC-4 (closer to active
    # trading season) — operator can override later if needed.
    return (datetime.now(timezone.utc).hour - 4) % 24


def _spread_bps(bid: float, ask: float, mid: float) -> Optional[float]:
    if bid <= 0 or ask <= 0 or mid <= 0:
        return None
    return (ask - bid) / mid * 10000.0


def _enrich_sync(symbol: str, base: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous core. Called from thread pool by the async wrapper."""
    from shared.market_data.webull_quotes import get_quotes_client  # noqa: WPS433

    client = get_quotes_client()
    if client is None:
        return base

    out = dict(base)
    sym = (symbol or "").upper()
    out["symbol"] = sym
    out["lane"] = "equity"
    out.setdefault("hour_et", _hour_et())

    snap = client.equity_snapshot(sym)
    if snap:
        price = _to_float(snap.get("price"))
        pre_close = _to_float(snap.get("pre_close"))
        bid = _to_float(snap.get("bid"))
        ask = _to_float(snap.get("ask"))
        change_ratio = _to_float(snap.get("change_ratio"))
        volume = _to_float(snap.get("volume"))
        if price > 0:
            out["price"] = price
            out["near_half_or_whole_dollar"] = _near_half_or_whole_dollar(price)
        if pre_close > 0 and price > 0:
            out["gap_pct"] = round((price - pre_close) / pre_close * 100.0, 4)
        if change_ratio:
            out["change_ratio"] = change_ratio
        if volume > 0:
            out["volume"] = volume
        sp = _spread_bps(bid, ask, price)
        if sp is not None:
            out["spread_bps"] = round(sp, 2)

    instr = client.instrument(sym)
    out["market_cap_band"] = _market_cap_band(sym, instr)
    if instr:
        out["fractionable"] = bool(instr.get("fractionable"))
        out["shortable"] = bool(instr.get("shortable"))
        out["exchange_code"] = instr.get("exchange_code")

    # Relative volume from the screener (only populated when the
    # ticker is on the most-active list — common for our universe).
    screener = client.most_active_map()
    if sym in screener:
        rv = _to_float(screener[sym].get("relative_volume_10d"))
        if rv > 0:
            out["relative_volume"] = rv

    # Bars for momentum / pullback / pattern
    bars = client.equity_bars(sym, timespan="M1", count=30)
    if bars:
        out["momentum_active"] = _momentum_active(bars)
        pl = _pullback_low(bars)
        if pl is not None:
            out["pullback_low"] = pl
        price = _to_float(out.get("price"))
        if price > 0:
            out["no_nearby_resistance"] = _no_nearby_resistance(bars, price)
        pattern = _detect_pattern(bars)
        if pattern:
            out["pattern"] = pattern

        # Parabolic-phase classification (operator directive 2026-06-11).
        # Teaches the brains to read the PAVS-style spike-and-fade by
        # stamping the phase + underlying velocity / VWAP-distance /
        # RVOL-acceleration measurements. The phase translates to the
        # existing `market_regime` slot so `base_labels.py` picks it up
        # (and applies score deltas) without any further wiring.
        from shared.snapshot_enrich.parabolic_phase import (  # noqa: WPS433
            classify_parabolic_phase, regime_from_phase,
        )
        phase, measurements = classify_parabolic_phase(bars, current_price=price)
        out["parabolic_phase"] = phase
        out["velocity_1m"] = measurements["velocity_1m"]
        out["velocity_5m"] = measurements["velocity_5m"]
        out["vwap_distance_pct"] = measurements["vwap_distance_pct"]
        out["rvol_acceleration"] = measurements["rvol_acceleration"]
        out["peak_drop_pct"] = measurements["peak_drop_pct"]
        regime = regime_from_phase(phase)
        if regime:
            # Override the cold-start "calm" only when phase is decisive.
            # `unknown` / `neutral` leave the regime slot untouched.
            out["market_regime"] = regime

    # Provenance — operator can filter "real_webull_data" vs cold-start
    out["webull_enriched"] = True
    out["real_market_data"] = True
    return out


async def enrich_equity_doctrine_snapshot(
    symbol: str, base_snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    """Async wrapper. Runs the sync Webull calls in the default
    executor so the brain tick loop never blocks on HTTP.

    Returns the original `base_snapshot` unchanged on ANY failure —
    fail-soft is the contract.
    """
    if not symbol:
        return base_snapshot
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _enrich_sync, symbol, base_snapshot)
    except Exception as e:  # noqa: BLE001
        logger.warning("equity enricher failed sym=%s err=%s", symbol, e)
        return base_snapshot
