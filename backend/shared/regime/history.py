"""Keyless daily-bar history fetchers for the Regime Engine.

Equity lane: Yahoo chart API (SPY/QQQ/^VIX) — keyless with UA header.
Crypto lane: Kraken public OHLC (XBTUSD/ETHUSD), ~720 daily bars.

Training source ≠ live source is fine per operator directive as long
as feature definitions stay normalized consistently.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("risedual.regime.history")

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) RISEDUAL/1.0"}
_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


async def yahoo_daily(symbol: str, range_: str = "5y") -> Optional[list[dict[str, Any]]]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_UA) as client:
            resp = await client.get(url, params={"range": range_, "interval": "1d"})
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("yahoo_daily %s failed: %s", symbol, exc)
        return None
    try:
        result = payload["chart"]["result"][0]
        ts = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        bars = []
        for i, t in enumerate(ts):
            c = quote["close"][i]
            if c is None:
                continue
            bars.append({
                "ts": int(t),
                "close": float(c),
                "volume": float(quote["volume"][i] or 0.0),
            })
        return bars if len(bars) >= 60 else None
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning("yahoo_daily %s parse failed: %s", symbol, exc)
        return None


async def kraken_daily(pair: str) -> Optional[list[dict[str, Any]]]:
    url = "https://api.kraken.com/0/public/OHLC"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, params={"pair": pair, "interval": 1440})
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("kraken_daily %s failed: %s", pair, exc)
        return None
    result = payload.get("result") or {}
    key = next((k for k in result if k != "last"), None)
    if not key:
        return None
    bars = []
    for row in result[key]:
        # [time, open, high, low, close, vwap, volume, count]
        bars.append({"ts": int(row[0]), "close": float(row[4]),
                     "volume": float(row[6])})
    return bars if len(bars) >= 60 else None
