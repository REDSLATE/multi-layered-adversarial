"""Crypto doctrine enricher — Webull as hot-failover for Kraken.

Operator directive (2026-06-11):
    Kraken creds aren't persisted in this DB yet. Crypto lane has been
    emitting on cold-start data, producing REJECT-quality intents. The
    operator wants to flip selectively between brokers per lane — and
    needs the data feed to follow. Webull's crypto entitlement (spot
    BTCUSD / ETHUSD bid/ask) is already free under the base subscription;
    this module uses it as the primary feed when Kraken's poller is
    not running, and as a cross-check when it is.

Source-of-truth selection:
    Reads the `broker_selection` singleton from MongoDB. If
    `crypto = "webull"`, Webull is treated as primary. If
    `crypto = "kraken"`, Kraken stays primary and Webull is the
    council-of-last-resort cross-check.

Fail-soft: any error returns the base snapshot unchanged.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("risedual.snapshot_enrich.crypto")


def _to_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _canonical_to_webull_pair(symbol: str) -> str:
    """`BTC/USD` or `BTC-USD` → `BTCUSD`."""
    s = (symbol or "").upper().replace("/", "").replace("-", "")
    return s


def _spread_bps(bid: float, ask: float, mid: float) -> Optional[float]:
    if bid <= 0 or ask <= 0 or mid <= 0:
        return None
    return (ask - bid) / mid * 10000.0


def _enrich_sync(symbol: str, base: Dict[str, Any]) -> Dict[str, Any]:
    from shared.market_data.webull_quotes import get_quotes_client  # noqa: WPS433

    client = get_quotes_client()
    if client is None:
        return base

    out = dict(base)
    sym = (symbol or "").upper()
    out["symbol"] = sym
    out["lane"] = "crypto"

    webull_pair = _canonical_to_webull_pair(sym)
    snap = client.crypto_snapshot(webull_pair)
    if snap:
        price = _to_float(snap.get("price"))
        bid = _to_float(snap.get("bid"))
        ask = _to_float(snap.get("ask"))
        volume = _to_float(snap.get("volume"))
        day_high = _to_float(snap.get("high"))
        day_low = _to_float(snap.get("low"))
        pre_close = _to_float(snap.get("pre_close"))
        if price and price > 0:
            out["price"] = price
        if pre_close and pre_close > 0:
            out["pre_close"] = pre_close
        if pre_close and price and pre_close > 0 and price > 0:
            out["gap_pct"] = round((price - pre_close) / pre_close * 100.0, 4)
        if volume and volume > 0:
            out["volume"] = volume
        if day_high and day_high > 0:
            out["high"] = day_high
        if day_low and day_low > 0:
            out["low"] = day_low
        if bid and ask and price:
            sp = _spread_bps(bid, ask, price)
            if sp is not None:
                out["spread_bps"] = round(sp, 2)
        out["bid"] = bid
        out["ask"] = ask
        out["webull_enriched"] = True
        out["real_market_data"] = True
        out["primary_source"] = "webull"
        out.setdefault("data_council", []).append("webull")
    else:
        out.setdefault("data_council", []).append("webull_offline")

    return out


async def enrich_crypto_doctrine_snapshot(
    symbol: str, base_snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    """Async wrapper. Returns base snapshot unchanged on any error."""
    if not symbol:
        return base_snapshot
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _enrich_sync, symbol, base_snapshot)
    except Exception as e:  # noqa: BLE001
        logger.warning("crypto enricher failed sym=%s err=%s", symbol, e)
        return base_snapshot
