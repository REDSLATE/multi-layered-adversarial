"""Public /scanner — pattern detection presets.

10 presets matching risedual.ai's `services/scanner_service.py`:

    macd_bullish_cross, macd_bearish_cross, bollinger_squeeze,
    ema_golden_cross, volume_spike, near_52w_high, near_52w_low,
    rsi_overbought, rsi_oversold, momentum_breakout.

Each preset's detection logic operates on MC's stored indicator
snapshots + recent OHLCV bars. Output shape (per risedual.ai's contract):

    {
      "preset_id": "macd_bullish_cross",
      "name": "MACD Bullish Cross",
      "category": "momentum",
      "signal": "bullish",
      "matches": [{"symbol": "NVDA", "strength": 75, "detail": "..."}],
      "scanned": 22,
      "matched": 3
    }
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from db import db
from namespaces import SHARED_INDICATOR_SNAPSHOTS, SHARED_OHLCV_BARS
from shared.indicators import build_snapshot

from .auth import PublicCaller, public_trust_required


router = APIRouter(tags=["public"])


PRESETS: dict[str, dict] = {
    "macd_bullish_cross":  {"name": "MACD Bullish Cross",  "category": "momentum",       "signal": "bullish"},
    "macd_bearish_cross":  {"name": "MACD Bearish Cross",  "category": "momentum",       "signal": "bearish"},
    "bollinger_squeeze":   {"name": "Bollinger Squeeze",   "category": "volatility",     "signal": "neutral"},
    "ema_golden_cross":    {"name": "EMA 9/21 Golden Cross","category": "trend",         "signal": "bullish"},
    "volume_spike":        {"name": "Volume Spike",        "category": "volume",         "signal": "neutral"},
    "near_52w_high":       {"name": "Near 52-Week High",   "category": "trend",          "signal": "bullish"},
    "near_52w_low":        {"name": "Near 52-Week Low",    "category": "mean_reversion", "signal": "bearish"},
    "rsi_overbought":      {"name": "RSI Overbought",      "category": "mean_reversion", "signal": "bearish"},
    "rsi_oversold":        {"name": "RSI Oversold",        "category": "mean_reversion", "signal": "bullish"},
    "momentum_breakout":   {"name": "Momentum Breakout",   "category": "momentum",       "signal": "bullish"},
}


def _last_n_closes(bars: list[dict], n: int) -> list[float]:
    return [float(b["c"]) for b in bars[-n:]]


async def _bars_for_scan(symbol: str, source: str, tf: str = "1d") -> list[dict]:
    return await db[SHARED_OHLCV_BARS].find(
        {"source": source, "symbol": symbol, "tf": tf}, {"_id": 0},
    ).sort("ts", 1).to_list(300)


def _detect_macd_cross(snap_indicators: dict, bars: list[dict],
                       direction: Literal["bullish", "bearish"]) -> tuple[bool, int, str]:
    """Detect a confirmed MACD cross in the last bar."""
    if len(bars) < 30:
        return False, 0, ""
    closes = _last_n_closes(bars, 30)
    # Use the snapshot's already-computed numbers for the latest bar,
    # and recompute just one bar back to detect the cross.
    macd_now = (snap_indicators.get("macd") or {}).get("macd")
    sig_now = (snap_indicators.get("macd") or {}).get("signal")
    if macd_now is None or sig_now is None:
        return False, 0, ""
    # Build prev snapshot from bars[:-1].
    prev_snap = build_snapshot([dict(b) for b in bars[:-1]])
    prev_macd = (prev_snap.get("macd") or {}).get("macd")
    prev_sig = (prev_snap.get("macd") or {}).get("signal")
    if prev_macd is None or prev_sig is None:
        return False, 0, ""

    if direction == "bullish":
        crossed = prev_macd < prev_sig and macd_now > sig_now
    else:
        crossed = prev_macd > prev_sig and macd_now < sig_now

    if not crossed:
        return False, 0, ""
    return True, 75, (
        f"MACD {macd_now:.4f} crossed "
        f"{'above' if direction == 'bullish' else 'below'} signal {sig_now:.4f}"
    )


def _detect_bollinger_squeeze(snap_indicators: dict) -> tuple[bool, int, str]:
    bb = snap_indicators.get("bbands") or {}
    width = bb.get("width_pct")
    if width is None:
        return False, 0, ""
    # Heuristic: width below 5% of the mid band = compressed.
    if width < 5.0:
        return True, round(80 - width * 10), f"BB width {width:.2f}% (compressed)"
    return False, 0, ""


def _detect_ema_golden_cross(snap_indicators: dict, bars: list[dict]) -> tuple[bool, int, str]:
    if len(bars) < 30:
        return False, 0, ""
    e_now = snap_indicators.get("ema") or {}
    e12 = e_now.get("12")
    e26 = e_now.get("26")
    if e12 is None or e26 is None:
        return False, 0, ""
    prev_snap = build_snapshot([dict(b) for b in bars[:-1]])
    p_ema = prev_snap.get("ema") or {}
    p12 = p_ema.get("12")
    p26 = p_ema.get("26")
    if p12 is None or p26 is None:
        return False, 0, ""
    if p12 < p26 and e12 > e26:
        return True, 70, f"EMA12 {e12:.4f} crossed above EMA26 {e26:.4f}"
    return False, 0, ""


def _detect_volume_spike(bars: list[dict]) -> tuple[bool, int, str]:
    if len(bars) < 21:
        return False, 0, ""
    vols = [float(b.get("v") or 0.0) for b in bars[-21:]]
    cur = vols[-1]
    avg = sum(vols[:-1]) / 20.0 if any(vols[:-1]) else 0.0
    if avg <= 0:
        return False, 0, ""
    ratio = cur / avg
    if ratio >= 2.0:
        return True, min(100, round(ratio * 25)), f"Volume {ratio:.2f}× 20-day avg"
    return False, 0, ""


def _detect_52w_extreme(bars: list[dict], near_high: bool) -> tuple[bool, int, str]:
    if len(bars) < 30:
        return False, 0, ""
    window = bars[-min(252, len(bars)):]
    highs = [float(b["h"]) for b in window]
    lows = [float(b["l"]) for b in window]
    last = float(bars[-1]["c"])
    hi = max(highs)
    lo = min(lows)
    if near_high:
        pct = (last / hi) * 100 if hi else 0
        if pct >= 97:
            return True, round(pct), f"Close {last:.2f} within {100 - pct:.2f}% of {hi:.2f}"
    else:
        pct = (last / lo) * 100 if lo else 0
        if pct <= 103 and lo > 0:
            return True, round(100 - (pct - 100)), f"Close {last:.2f} within {pct - 100:.2f}% of {lo:.2f}"
    return False, 0, ""


def _detect_rsi(snap_indicators: dict, overbought: bool) -> tuple[bool, int, str]:
    r = snap_indicators.get("rsi14")
    if r is None:
        return False, 0, ""
    if overbought and r >= 70:
        return True, round(min(100, (r - 70) * 5 + 60)), f"RSI {r:.1f} ≥ 70"
    if not overbought and r <= 30:
        return True, round(min(100, (30 - r) * 5 + 60)), f"RSI {r:.1f} ≤ 30"
    return False, 0, ""


def _detect_momentum_breakout(snap_indicators: dict, bars: list[dict]) -> tuple[bool, int, str]:
    if len(bars) < 21:
        return False, 0, ""
    closes = [float(b["c"]) for b in bars[-21:]]
    last = closes[-1]
    rolling_high = max(closes[:-1])
    if last > rolling_high:
        gain = (last / rolling_high - 1.0) * 100
        return True, min(100, 60 + round(gain * 5)), f"Close {last:.2f} broke 20-bar high {rolling_high:.2f} (+{gain:.2f}%)"
    return False, 0, ""


async def _scan_preset(preset_id: str) -> dict:
    if preset_id not in PRESETS:
        raise HTTPException(status_code=404, detail=f"unknown preset {preset_id!r}")
    meta = PRESETS[preset_id]

    # Scan over every (source, symbol) with a daily-tf snapshot.
    snaps = await db[SHARED_INDICATOR_SNAPSHOTS].find(
        {"tf": "1d"}, {"_id": 0},
    ).to_list(500)
    if not snaps:
        # Fall back to hourly if nothing has 1d coverage yet.
        snaps = await db[SHARED_INDICATOR_SNAPSHOTS].find(
            {"tf": "1h"}, {"_id": 0},
        ).to_list(500)

    matches: list[dict] = []
    scanned = 0
    for snap in snaps:
        scanned += 1
        symbol = snap["symbol"]
        source = snap["source"]
        ind = snap.get("indicators") or {}
        bars = await _bars_for_scan(symbol, source, tf=snap.get("tf", "1d"))

        hit, strength, detail = False, 0, ""
        if preset_id == "macd_bullish_cross":
            hit, strength, detail = _detect_macd_cross(ind, bars, "bullish")
        elif preset_id == "macd_bearish_cross":
            hit, strength, detail = _detect_macd_cross(ind, bars, "bearish")
        elif preset_id == "bollinger_squeeze":
            hit, strength, detail = _detect_bollinger_squeeze(ind)
        elif preset_id == "ema_golden_cross":
            hit, strength, detail = _detect_ema_golden_cross(ind, bars)
        elif preset_id == "volume_spike":
            hit, strength, detail = _detect_volume_spike(bars)
        elif preset_id == "near_52w_high":
            hit, strength, detail = _detect_52w_extreme(bars, near_high=True)
        elif preset_id == "near_52w_low":
            hit, strength, detail = _detect_52w_extreme(bars, near_high=False)
        elif preset_id == "rsi_overbought":
            hit, strength, detail = _detect_rsi(ind, overbought=True)
        elif preset_id == "rsi_oversold":
            hit, strength, detail = _detect_rsi(ind, overbought=False)
        elif preset_id == "momentum_breakout":
            hit, strength, detail = _detect_momentum_breakout(ind, bars)

        if hit:
            matches.append({"symbol": symbol, "strength": strength, "detail": detail})

    matches.sort(key=lambda r: r["strength"], reverse=True)
    return {
        "preset_id": preset_id,
        "name": meta["name"],
        "category": meta["category"],
        "signal": meta["signal"],
        "matches": matches,
        "scanned": scanned,
        "matched": len(matches),
    }


@router.get("/public/scanner/presets")
async def list_presets(caller: PublicCaller = Depends(public_trust_required)):
    return {
        "presets": [
            {"preset_id": pid, **meta} for pid, meta in PRESETS.items()
        ],
        "count": len(PRESETS),
        "tier": caller.tier,
    }


@router.get("/public/scanner/scan")
async def scan(
    preset_id: str = Query(..., description="one of the preset_id values from /public/scanner/presets"),
    caller: PublicCaller = Depends(public_trust_required),
):
    return await _scan_preset(preset_id)
