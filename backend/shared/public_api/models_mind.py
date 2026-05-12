"""Public /models-mind/{symbol} — feature panel per symbol.

The original risedual.ai UI shows ~10 feature bars (score_2W,
distance_from_mw, macro_regime_flag, atr_id, earnings_proximity,
momentum_3d, sector_rs, pattern_score, rsi_id, vol_zscore). The names
didn't exist in risedual.ai's actual backend — they were aspirational.

MC defines them canonically here, computed from real data where MC has
it. Each feature returns a normalized score in [0, 100] (so the UI's
bar widget renders without translation) plus a raw value the UI can
display alongside.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from db import db
from namespaces import SHARED_INDICATOR_SNAPSHOTS, SHARED_OHLCV_BARS

from .auth import PublicCaller, public_trust_required


router = APIRouter(tags=["public"])


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _normalize_rsi(rsi: Optional[float]) -> Optional[dict]:
    if rsi is None:
        return None
    # Distance from midpoint (50) → score 0-100, with overbought/oversold extremes
    # mapped to high scores (interesting to model).
    score = round(min(100, abs(rsi - 50) * 2))
    return {"score": score, "value": round(rsi, 2), "label": _rsi_label(rsi)}


def _rsi_label(rsi: float) -> str:
    if rsi >= 70:
        return "overbought"
    if rsi <= 30:
        return "oversold"
    if rsi >= 55:
        return "trending_up"
    if rsi <= 45:
        return "trending_down"
    return "neutral"


def _normalize_atr_pct(atr_pct: Optional[float]) -> Optional[dict]:
    if atr_pct is None:
        return None
    # 0% = dead. 5% = explosive. Mostly we see 0.5-2.5%.
    score = round(min(100, atr_pct * 25))
    return {"score": score, "value": round(atr_pct, 2)}


def _normalize_distance(last_close: Optional[float], sma20: Optional[float],
                        atr14: Optional[float]) -> Optional[dict]:
    if last_close is None or sma20 is None:
        return None
    raw = last_close - sma20
    # Express in ATR units when ATR is available; otherwise as % of SMA.
    if atr14 and atr14 > 0:
        z = raw / atr14
        score = round(min(100, abs(z) * 30))
        return {"score": score, "value": round(z, 3), "units": "atr"}
    pct = (raw / sma20) * 100 if sma20 else 0
    score = round(min(100, abs(pct) * 5))
    return {"score": score, "value": round(pct, 2), "units": "pct"}


def _normalize_bb_position(pos: Optional[float]) -> Optional[dict]:
    if pos is None:
        return None
    # 0 = at lower band, 0.5 = mid, 1 = upper. Both extremes interesting.
    score = round(abs(pos - 0.5) * 200)
    score = max(0, min(100, score))
    return {"score": score, "value": round(pos, 3)}


def _normalize_macd_hist(hist: Optional[float], last_close: Optional[float]) -> Optional[dict]:
    if hist is None:
        return None
    # Scale by price so % comparable across symbols.
    if last_close and last_close > 0:
        bps = abs(hist) / last_close * 10000  # basis points
        score = round(min(100, bps))
    else:
        score = round(min(100, abs(hist) * 100))
    return {"score": score, "value": round(hist, 4)}


async def _momentum_3d(symbol: str, source: str, tf: str) -> Optional[dict]:
    bars = await db[SHARED_OHLCV_BARS].find(
        {"source": source, "symbol": symbol, "tf": tf}, {"_id": 0, "c": 1, "ts": 1},
    ).sort("ts", -1).to_list(5)
    if len(bars) < 4:
        return None
    bars.reverse()
    pct = (float(bars[-1]["c"]) - float(bars[-4]["c"])) / float(bars[-4]["c"]) * 100
    score = round(min(100, abs(pct) * 5))
    return {"score": score, "value": round(pct, 2), "direction": "up" if pct >= 0 else "down"}


async def _vol_zscore(symbol: str, source: str, tf: str) -> Optional[dict]:
    bars = await db[SHARED_OHLCV_BARS].find(
        {"source": source, "symbol": symbol, "tf": tf}, {"_id": 0, "v": 1, "ts": 1},
    ).sort("ts", -1).to_list(21)
    if len(bars) < 21:
        return None
    bars.reverse()
    vols = [float(b.get("v") or 0.0) for b in bars]
    if not any(vols[:-1]):
        return None
    avg = sum(vols[:-1]) / 20.0
    var = sum((v - avg) ** 2 for v in vols[:-1]) / 20.0
    sd = var ** 0.5 if var > 0 else 0
    if sd == 0:
        return None
    z = (vols[-1] - avg) / sd
    score = round(min(100, abs(z) * 25))
    return {"score": score, "value": round(z, 2)}


def _pattern_score(rsi: Optional[float], bb_pos: Optional[float],
                   macd_hist: Optional[float]) -> dict:
    """Aggregate pattern strength across RSI extreme, BB extreme, and MACD divergence."""
    score = 0
    n = 0
    if rsi is not None:
        score += min(100, abs(rsi - 50) * 2)
        n += 1
    if bb_pos is not None:
        score += min(100, abs(bb_pos - 0.5) * 200)
        n += 1
    if macd_hist is not None:
        score += min(100, abs(macd_hist) * 100)
        n += 1
    final = round(score / n) if n else 0
    return {"score": final, "value": final}


async def _resolve_symbol(symbol: str) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    """Return (snapshot, source, tf) for the preferred feed of the symbol.
    Preference: 1d daily on any feeder, falling back to 1h."""
    for tf in ("1d", "1h", "4h", "15m", "5m", "1m"):
        snap = await db[SHARED_INDICATOR_SNAPSHOTS].find_one(
            {"symbol": symbol, "tf": tf}, {"_id": 0},
        )
        if snap:
            return snap, snap["source"], tf
    return None, None, None


@router.get("/public/models-mind/{symbol:path}")
async def get_models_mind(
    symbol: str,
    caller: PublicCaller = Depends(public_trust_required),
):
    """Return the 10-feature panel for `symbol`. Features MC can't
    compute (earnings_proximity, sector_rs) return null + a `coverage`
    flag so the UI can grey them out."""
    sym = symbol.upper()
    snap, source, tf = await _resolve_symbol(sym)
    if not snap:
        raise HTTPException(
            status_code=404,
            detail=f"no technical data for {sym}",
        )

    ind = snap.get("indicators") or {}
    last_close = ind.get("last_close")
    sma20 = (ind.get("sma") or {}).get("20")
    atr14 = ind.get("atr14")
    atr_pct = ind.get("atr14_pct")
    rsi = ind.get("rsi14")
    bb_pos = (ind.get("bbands") or {}).get("position")
    macd_hist = (ind.get("macd") or {}).get("hist")

    features = {
        "score_2W": {
            # Aggregate "interestingness" score over recent window.
            "score": round(min(100, ((abs((rsi or 50) - 50) * 2 +
                                       (abs((bb_pos or 0.5) - 0.5) * 200)) / 2))) if rsi is not None and bb_pos is not None else None,
            "value": None,
        },
        "distance_from_mw": _normalize_distance(last_close, sma20, atr14),
        "macro_regime_flag": {
            # Coarse regime tag derived from MACD direction + RSI bias.
            "score": round(min(100, abs((rsi or 50) - 50) * 2)),
            "value": (
                "risk_on" if (macd_hist or 0) > 0 and (rsi or 50) >= 50
                else "risk_off" if (macd_hist or 0) < 0 and (rsi or 50) < 50
                else "neutral"
            ),
        },
        "atr_id": _normalize_atr_pct(atr_pct),
        "earnings_proximity": {
            "score": None,
            "value": None,
            "coverage": "not_wired",
        },
        "momentum_3d": await _momentum_3d(sym, source, tf),
        "sector_rs": {
            "score": None,
            "value": None,
            "coverage": "not_wired",
        },
        "pattern_score": _pattern_score(rsi, bb_pos, macd_hist),
        "rsi_id": _normalize_rsi(rsi),
        "vol_zscore": await _vol_zscore(sym, source, tf),
    }

    return {
        "symbol": sym,
        "source": source,
        "tf": tf,
        "last_close": last_close,
        "last_bar_ts": snap.get("last_bar_ts"),
        "features": features,
        "computed_at": snap.get("computed_at"),
        "tier": caller.tier,
    }
