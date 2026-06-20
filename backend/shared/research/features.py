"""Research Layer feature builder.

Composes a `MarketFeatureFrame` from a window of OHLCV bars by reusing
the existing `shared.indicators` math + the small VWAP/rvol helpers
local to this package. No I/O — `bars` is supplied by the caller (the
router layer is responsible for sourcing them from Mongo, the brain's
cache, or a backtest fixture).
"""
from __future__ import annotations

from typing import Optional

from shared.indicators import build_snapshot

from .indicators import rvol, vwap
from .schemas import MarketFeatureFrame


def build_features(
    symbol: str,
    lane: str,
    bars: list[dict],
    spread_bps: Optional[float] = None,
) -> MarketFeatureFrame:
    """Build a feature frame from a window of OHLCV bars.

    Args:
        symbol: ticker / pair (e.g. "AAPL", "BTC/USD").
        lane:   "equity" | "crypto".
        bars:   oldest → newest OHLCV dicts (keys o, h, l, c, v).
                Must be sorted asc by timestamp; caller's contract.
        spread_bps: optional latest bid/ask spread in basis points.
                Forwarded straight into the frame so wide-spread
                penalties stay deterministic.

    Returns:
        A `MarketFeatureFrame`. Indicators are None on cold-start
        windows that don't have enough bars yet — strategies are
        expected to skip cleanly when their required fields are None.
    """
    snap = build_snapshot(bars or [])
    if not snap.get("ready"):
        # Cold start — return a sparse frame so callers can still log
        # *why* a strategy returned HOLD ("not enough bars yet").
        last_close = bars[-1]["c"] if bars else 0.0
        last_vol = bars[-1].get("v") if bars else None
        return MarketFeatureFrame(
            symbol=symbol,
            lane=lane,
            close=float(last_close or 0.0),
            volume=(float(last_vol) if last_vol is not None else None),
            spread_bps=spread_bps,
            bars_seen=len(bars or []),
        )

    macd_block = snap.get("macd") or {}
    return MarketFeatureFrame(
        symbol=symbol,
        lane=lane,
        close=float(snap["last_close"]),
        vwap=vwap(bars),
        rsi_14=snap.get("rsi14"),
        macd=macd_block.get("macd"),
        macd_signal=macd_block.get("signal"),
        macd_hist=macd_block.get("hist"),
        atr_14=snap.get("atr14"),
        atr_14_pct=snap.get("atr14_pct"),
        volume=(float(bars[-1]["v"]) if bars and bars[-1].get("v") is not None else None),
        rvol=rvol(bars, 20),
        spread_bps=spread_bps,
        bars_seen=int(snap.get("bars_seen", len(bars or []))),
    )
