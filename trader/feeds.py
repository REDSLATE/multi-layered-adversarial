"""Live market data — Kraken (crypto) + Yahoo (equity).

Honest source-of-truth pulls. No mocks. No simulated bars. If the
upstream API fails, the trader cycle returns None and the loop skips
this lane this iteration. Better to miss a tick than trade on stale
data.

Each fetcher returns a dict shaped for the brain layer:
    {
        "lane":        "equity" | "crypto",
        "symbol":      "BTC/USD" or "TSLA",
        "last_price":  float,
        "high_20":     float (20-period high, optional),
        "low_20":      float (20-period low, optional),
        "sma_20":      float,
        "rsi_14":      float (0-100),
        "macd":        float,
        "macd_signal": float,
        "bb_position": float (0-1, optional),
        "ts":          iso datetime,
    }
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx


logger = logging.getLogger("trader.feeds")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sma(series: list[float], n: int) -> Optional[float]:
    if len(series) < n:
        return None
    return sum(series[-n:]) / n


def _rsi(closes: list[float], n: int = 14) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(-n, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-diff)
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _ema(series: list[float], n: int) -> Optional[float]:
    if len(series) < n:
        return None
    k = 2 / (n + 1)
    ema = sum(series[:n]) / n
    for v in series[n:]:
        ema = v * k + ema * (1 - k)
    return ema


def _macd(closes: list[float]) -> tuple[Optional[float], Optional[float]]:
    """Returns (macd, signal). Uses 12/26/9 standard."""
    if len(closes) < 35:
        return None, None
    ema12 = _ema(closes[-30:], 12)
    ema26 = _ema(closes[-30:], 26)
    if ema12 is None or ema26 is None:
        return None, None
    macd = ema12 - ema26
    # signal = 9-EMA of macd history (approx — use last 9 macd values)
    macd_series = []
    for i in range(9, 0, -1):
        sub = closes[: len(closes) - i + 1]
        e12 = _ema(sub[-30:], 12)
        e26 = _ema(sub[-30:], 26)
        if e12 is not None and e26 is not None:
            macd_series.append(e12 - e26)
    if len(macd_series) < 9:
        return macd, None
    sig = _ema(macd_series, 9)
    return macd, sig


# ── Kraken OHLC ──────────────────────────────────────────────────
async def fetch_kraken(pair: str = "XBTUSD") -> Optional[dict]:
    """Pull Kraken 1h OHLC bars (last 30) and build a snapshot."""
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=60"
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(url)
            r.raise_for_status()
            j = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("kraken fetch failed pair=%s err=%s", pair, e)
        return None
    if j.get("error"):
        logger.warning("kraken error pair=%s err=%s", pair, j["error"])
        return None
    result = j.get("result") or {}
    # Kraken returns the data keyed by the canonical pair name,
    # which may differ from the request (e.g. XBTUSD → XXBTZUSD).
    bars_key = next((k for k in result if k != "last"), None)
    if not bars_key:
        return None
    bars = result.get(bars_key) or []
    # bar = [ts, open, high, low, close, vwap, volume, count]
    closes = [float(b[4]) for b in bars]
    highs = [float(b[2]) for b in bars]
    lows = [float(b[3]) for b in bars]
    if not closes:
        return None
    last_price = closes[-1]
    macd, macd_signal = _macd(closes)
    return {
        "lane": "crypto",
        "symbol": pair,
        "last_price": last_price,
        "high_20": max(highs[-20:]) if len(highs) >= 20 else None,
        "low_20": min(lows[-20:]) if len(lows) >= 20 else None,
        "sma_20": _sma(closes, 20),
        "rsi_14": _rsi(closes, 14),
        "macd": macd,
        "macd_signal": macd_signal,
        "ts": _now_iso(),
    }


# ── Yahoo / equity ───────────────────────────────────────────────
async def fetch_equity(ticker: str = "TSLA") -> Optional[dict]:
    """Pull Yahoo daily OHLC (last 60d) and build a snapshot. Yahoo
    is the unauthenticated free source and is sufficient for the
    indicators the brains need. Webull's public quote API can be
    swapped in later if we need pre-market / extended-hours data."""
    end = int(datetime.now(timezone.utc).timestamp())
    start = end - (60 * 24 * 3600)  # 60 days
    url = (
        f"https://query1.finance.yahoo.com/v7/finance/chart/{ticker}"
        f"?period1={start}&period2={end}&interval=1d"
    )
    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as c:
            r = await c.get(url)
            r.raise_for_status()
            j = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("yahoo fetch failed ticker=%s err=%s", ticker, e)
        return None
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        return None
    quote = (res[0].get("indicators") or {}).get("quote") or [{}]
    closes = [c for c in (quote[0].get("close") or []) if c is not None]
    highs = [h for h in (quote[0].get("high") or []) if h is not None]
    lows = [l for l in (quote[0].get("low") or []) if l is not None]
    if not closes:
        return None
    last_price = closes[-1]
    macd, macd_signal = _macd(closes)
    return {
        "lane": "equity",
        "symbol": ticker.upper(),
        "last_price": last_price,
        "high_20": max(highs[-20:]) if len(highs) >= 20 else None,
        "low_20": min(lows[-20:]) if len(lows) >= 20 else None,
        "sma_20": _sma(closes, 20),
        "rsi_14": _rsi(closes, 14),
        "macd": macd,
        "macd_signal": macd_signal,
        "ts": _now_iso(),
    }
