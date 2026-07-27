"""Crypto snapshot enrichment — MC market truth BEFORE doctrine grading.

Operator doctrine (2026-07-27):
    Real unfavorable market data → REJECT
    Missing or stale market data → NO_DATA
Defaults like spread_bps=9999 / volatility=0 / volume=0 manufacture a
bearish, untradeable market out of missing information. This module
fills the load-bearing fields from MC's own sources so the doctrine
grades real market structure — and stamps provenance so every doctrine
result is auditable.

Fallback ladder per field group:
  quotes/volume:  brain-provided → Kraken public ticker → recent
                  cached ticker → NO_DATA
  vol/trend:      brain-provided → MC 5m bars (shared_ohlcv_bars) →
                  NO_DATA
Brain-provided values always win; MC only fills what's missing.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger("risedual.crypto_snapshot_enrichment")

MAX_AGE_MS: float = float(os.environ.get("CRYPTO_SNAPSHOT_MAX_AGE_MS", "60000"))
TICKER_TIMEOUT_S: float = float(os.environ.get("CRYPTO_TICKER_TIMEOUT_S", "3.0"))
# Fresh window: a ticker younger than this is served from cache
# without a live fetch — keeps burst ingest (one pulse tick can emit
# dozens of intents) inside Kraken's public rate limits (~1 req/s).
TICKER_FRESH_S: float = float(os.environ.get("CRYPTO_TICKER_FRESH_S", "15"))
MIN_BARS: int = int(os.environ.get("CRYPTO_ENRICH_MIN_BARS", "8"))
BARS_WINDOW: int = 12  # 12 × 5m = 1h

REQUIRED_FIELDS = ("bid", "ask", "spread_bps", "volume_24h_usd",
                   "volatility_1h", "trend_strength")

# recent-ticker cache: pair → {at (monotonic), data}
_ticker_cache: dict[str, dict[str, Any]] = {}


def reset_for_tests() -> None:
    _ticker_cache.clear()


def _valid(v: Any) -> bool:
    try:
        return v is not None and float(v) == float(v)
    except (TypeError, ValueError):
        return False


async def _fetch_kraken_ticker(symbol: str) -> Optional[dict]:
    """Public Kraken ticker → {bid, ask, volume_24h_usd}. No auth."""
    from shared.crypto.kraken import to_kraken_pair  # noqa: WPS433
    pair = to_kraken_pair(symbol)
    if not pair:
        return None
    async with httpx.AsyncClient(timeout=TICKER_TIMEOUT_S) as client:
        r = await client.get(
            "https://api.kraken.com/0/public/Ticker", params={"pair": pair},
        )
        r.raise_for_status()
        data = r.json()
    if data.get("error"):
        raise RuntimeError(f"kraken ticker error: {data['error']}")
    result = data.get("result") or {}
    if not result:
        return None
    _, payload = next(iter(result.items()))
    bid = float(payload["b"][0])
    ask = float(payload["a"][0])
    vol_base_24h = float(payload["v"][1])
    vwap_24h = float(payload["p"][1])
    return {
        "bid": bid,
        "ask": ask,
        "volume_24h_usd": round(vol_base_24h * vwap_24h, 2),
    }


async def _ticker_with_ladder(symbol: str) -> tuple[Optional[dict], str, float]:
    """(data, source, age_ms) — fresh cache → live → stale cache →
    (None, NO_DATA)."""
    now = time.monotonic()
    cached = _ticker_cache.get(symbol)
    if cached:
        age_s = now - cached["at"]
        if age_s <= TICKER_FRESH_S:
            return cached["data"], "CACHE", round(age_s * 1000.0, 1)
    try:
        live = await _fetch_kraken_ticker(symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("crypto ticker fetch failed %s: %s", symbol, exc)
        live = None
    if live is not None:
        _ticker_cache[symbol] = {"at": now, "data": live}
        return live, "KRAKEN_PUBLIC", 0.0
    if cached:
        age_ms = (now - cached["at"]) * 1000.0
        if age_ms <= MAX_AGE_MS:
            return cached["data"], "CACHE", round(age_ms, 1)
    return None, "NO_DATA", -1.0


async def _bars_features(symbol: str) -> tuple[Optional[float], Optional[float], int]:
    """(volatility_1h, trend_strength, bars_used) from MC 5m bars."""
    try:
        from db import db  # noqa: WPS433
        cur = db["shared_ohlcv_bars"].find(
            {"symbol": symbol, "tf": "5m"},
            {"_id": 0, "o": 1, "h": 1, "l": 1, "c": 1, "ts": 1},
        ).sort("ts", -1).limit(BARS_WINDOW)
        bars = [b async for b in cur]
    except Exception as exc:  # noqa: BLE001
        logger.warning("crypto bars fetch failed %s: %s", symbol, exc)
        return None, None, 0
    bars = [b for b in bars if _valid(b.get("c")) and float(b["c"]) > 0]
    if len(bars) < MIN_BARS:
        return None, None, len(bars)
    bars.reverse()  # chronological
    closes = [float(b["c"]) for b in bars]
    highs = [float(b.get("h") or b["c"]) for b in bars]
    lows = [float(b.get("l") or b["c"]) for b in bars]
    last = closes[-1]
    volatility_1h = max(0.0, (max(highs) - min(lows)) / last)
    # trend efficiency: |net move| / path length (0 = chop, 1 = clean)
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    trend_strength = min(1.0, net / path) if path > 0 else 0.0
    return round(volatility_1h, 6), round(trend_strength, 4), len(bars)


async def enrich_crypto_snapshot(
    snapshot: dict, *, symbol: str,
) -> tuple[dict, dict]:
    """Returns (enriched_snapshot, diagnostics). Brain values win;
    MC fills missing. Missing/stale required data → enrichment_status
    NO_DATA (the labeler returns NO_DATA, never REJECT, on that)."""
    t0 = time.monotonic()
    enriched = dict(snapshot or {})
    diag: dict[str, Any] = {"symbol": symbol, "filled": [], "ladder": []}

    # ── quotes + volume ──
    need_quote = not all(_valid(enriched.get(k)) and float(enriched[k]) > 0
                         for k in ("bid", "ask"))
    need_vol = not (_valid(enriched.get("volume_24h_usd"))
                    and float(enriched["volume_24h_usd"]) > 0)
    source, age_ms = "BRAIN", 0.0
    if need_quote or need_vol:
        tick, source, age_ms = await _ticker_with_ladder(symbol)
        diag["ladder"].append({"source": source, "age_ms": age_ms})
        if tick:
            for k in ("bid", "ask", "volume_24h_usd"):
                if not (_valid(enriched.get(k)) and float(enriched.get(k) or 0) > 0):
                    enriched[k] = tick[k]
                    diag["filled"].append(k)

    # quote sanity — invalid quotes are NO_DATA, never market truth
    bid = float(enriched.get("bid") or 0)
    ask = float(enriched.get("ask") or 0)
    quotes_ok = bid > 0 and ask > 0 and ask >= bid

    # ── spread from real quotes (brain-provided spread wins) ──
    existing_spread = enriched.get("spread_bps")
    if quotes_ok and (not _valid(existing_spread)
                      or float(existing_spread) >= 9999.0):
        mid = (bid + ask) / 2.0
        enriched["spread_bps"] = round((ask - bid) / mid * 10_000.0, 2)
        enriched["spread_source"] = (
            "MC_KRAKEN_PUBLIC" if source == "KRAKEN_PUBLIC"
            else "MC_CACHE" if source == "CACHE" else "MC_DERIVED_BID_ASK"
        )
        diag["filled"].append("spread_bps")
    elif _valid(existing_spread) and float(existing_spread) < 9999.0:
        enriched.setdefault("spread_source", "BRAIN")

    # ── volatility + trend from MC bars ──
    bars_used = 0
    if not (_valid(enriched.get("volatility_1h"))
            and _valid(enriched.get("trend_strength"))):
        vol_1h, trend, bars_used = await _bars_features(symbol)
        if vol_1h is not None and not _valid(enriched.get("volatility_1h")):
            enriched["volatility_1h"] = vol_1h
            diag["filled"].append("volatility_1h")
        if trend is not None and not _valid(enriched.get("trend_strength")):
            enriched["trend_strength"] = trend
            diag["filled"].append("trend_strength")

    # ── status: missing/stale/invalid → NO_DATA ──
    missing = [f for f in REQUIRED_FIELDS if not _valid(enriched.get(f))]
    stale = age_ms is not None and age_ms > MAX_AGE_MS
    if not quotes_ok and "bid" not in missing:
        missing = list(dict.fromkeys(missing + ["bid", "ask"]))

    status = "ENRICHED"
    if missing or stale or not quotes_ok:
        status = "NO_DATA"
        # RoadGuard compat: spread_bps must exist on the doc; the
        # sentinel is now ALWAYS paired with NO_DATA so it can never
        # be graded as a real market condition.
        if not _valid(enriched.get("spread_bps")):
            enriched["spread_bps"] = 9999.0
            enriched["spread_source"] = "MC_SENTINEL"

    enriched["snapshot_source"] = source
    enriched["snapshot_age_ms"] = round(max(0.0, age_ms), 1)
    enriched["snapshot_enriched_at"] = time.time()
    enriched["bars_used"] = bars_used
    enriched["enrichment_status"] = status
    enriched["missing_required_fields"] = missing
    diag["status"] = status
    diag["elapsed_ms"] = round((time.monotonic() - t0) * 1000.0, 2)
    return enriched, diag
