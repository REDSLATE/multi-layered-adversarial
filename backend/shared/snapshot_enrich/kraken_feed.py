"""Kraken public-ticker bid/ask feed for crypto spread enrichment.

Why this module exists (2026-02-21):
    Crypto execution routes to Kraken (`LANE_BROKER_REGISTRY['crypto']
    = 'kraken'` + `broker_selection.crypto = 'kraken'`). But the
    spread doctrine that decides whether a crypto intent is even
    *tradeable* has been reading bid/ask from Webull's crypto feed —
    a thin book whose entitlement frequently returns price-only and
    forces a hardcoded `spread_bps = 30.0` fallback. Net effect:
    20,570 crypto intents · 0 executed.

    This fetcher pulls bid/ask straight from Kraken's PUBLIC ticker
    (no auth needed, no rate-limit risk at 5s cache TTL) so the
    spread the gates evaluate matches the book the broker will hit.

Contract:
    * `kraken_bidask(symbol)` returns
      `{"bid": float, "ask": float, "price": float, "spread_bps":
       float, "src": "kraken"}` or None on any failure.
    * Per-process 5-second cache. Bursts of crypto intents on the
      same symbol hit Kraken at most once every 5 s.
    * Fail-soft: any error returns None — the caller MUST fall
      through to its existing feed. Never raises.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import httpx

from shared.crypto.kraken import KRAKEN_BASE, USER_AGENT, to_kraken_pair


logger = logging.getLogger("risedual.snapshot_enrich.kraken_feed")


# ── tiny in-process cache ────────────────────────────────────────────
# key = kraken_pair (e.g. "XBTUSD"), value = (fetched_at_monotonic, payload)
_CACHE_TTL_SEC = 5.0
_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}


def _to_float(x: Any) -> Optional[float]:
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _spread_bps(bid: float, ask: float, mid: float) -> Optional[float]:
    if bid <= 0 or ask <= 0 or mid <= 0:
        return None
    return (ask - bid) / mid * 10000.0


async def kraken_bidask(symbol: str) -> Optional[Dict[str, Any]]:
    """Fetch (bid, ask, last, spread_bps) from Kraken's public Ticker.

    Args:
        symbol: canonical pair (e.g. "BTC/USD", "ETH-USD") OR a
            Kraken-native altname (e.g. "XBTUSD"). `to_kraken_pair`
            normalises both.

    Returns:
        Dict with keys {bid, ask, price, spread_bps, src} on success.
        None on missing fields, HTTP failure, or any exception.

    Doctrine: fail-soft. Caller (`crypto_doctrine.py`) treats None
    as "no Kraken data — use Webull fallback".
    """
    if not symbol:
        return None
    kraken_pair = to_kraken_pair(symbol) if "/" in symbol or "-" in symbol else symbol

    now = time.monotonic()
    cached = _cache.get(kraken_pair)
    if cached and (now - cached[0]) < _CACHE_TTL_SEC:
        return cached[1]

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(
                f"{KRAKEN_BASE}/0/public/Ticker",
                params={"pair": kraken_pair},
                headers={"User-Agent": USER_AGENT},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("kraken_bidask fetch failed pair=%s err=%s", kraken_pair, e)
        return None

    if data.get("error"):
        logger.warning("kraken_bidask error pair=%s err=%s", kraken_pair, data["error"])
        return None
    result = data.get("result") or {}
    if not result:
        return None

    # Kraken returns one entry per pair; grab the first.
    _, payload = next(iter(result.items()))
    # Ticker payload shape:
    #   a = [ask_price, whole_lot, lot_volume]
    #   b = [bid_price, whole_lot, lot_volume]
    #   c = [last_price, lot_volume]
    ask = _to_float((payload.get("a") or [None])[0])
    bid = _to_float((payload.get("b") or [None])[0])
    last = _to_float((payload.get("c") or [None])[0])
    if not (bid and ask and last):
        return None

    sp = _spread_bps(bid, ask, last)
    if sp is None:
        return None

    out = {
        "bid": bid,
        "ask": ask,
        "price": last,
        "spread_bps": round(sp, 2),
        "src": "kraken",
    }
    _cache[kraken_pair] = (now, out)
    return out
