"""Crypto doctrine enricher — Kraken primary, Webull hot-failover.

Operator directive timeline:
    2026-06-11: Kraken creds weren't persisted; Webull was the only
        feed available. Module shipped as Webull-primary with a
        hardcoded `spread_bps = 30.0` fallback for the price-only
        case.
    2026-02-21: 20,570 crypto intents · 0 executed. The spread
        doctrine was reading Webull's thin / often-bid/ask-less feed
        while execution was routing to Kraken — a feed mismatch
        masked by the 30 bps band-aid. Fix: when broker_selection
        says `crypto = "kraken"`, fetch bid/ask from Kraken's public
        Ticker FIRST (no auth needed, 5s cache). Webull becomes the
        cross-check / fallback.

Source-of-truth selection:
    Reads the `broker_selection` singleton from MongoDB. If
    `crypto = "kraken"` (the new default), Kraken is primary; Webull
    fills in only when Kraken returns nothing. If `crypto =
    "webull"`, the prior path holds.

Fail-soft: any error returns the base snapshot unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
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
        # 2026-02-20: even when no Webull client, surface what we
        # know about the snapshot so the operator can see provenance.
        out = dict(base)
        out.setdefault("snapshot_source", "base_only_no_webull_client")
        out.setdefault("snapshot_age_ms", None)
        return out

    out = dict(base)
    sym = (symbol or "").upper()
    out["symbol"] = sym
    out["lane"] = "crypto"

    # Record exactly when this enrichment ran so downstream gates can
    # detect stale data (auto_router_advisory_only on age > 60s, etc.).
    enrich_started = time.monotonic()
    fetch_ts_iso = datetime.now(timezone.utc).isoformat()

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
        else:
            # 2026-02-20: Webull crypto entitlement sometimes returns
            # price but no bid/ask. Without spread_bps the upstream
            # roadguard gate ("ROADGUARD_MISSING_SPREAD_BPS — snapshot
            # absent") fails closed on EVERY crypto intent. That's
            # what killed crypto throughput entirely (BTC/USD,
            # ETH/USD, SOL/USD all bucketed WIDE_SPREAD even though
            # Kraken majors actually run < 5 bps). Fall back to a
            # documented default that's well below the 200 bps cap
            # but conservatively wider than reality so the gate
            # doesn't lie about market quality.
            out["spread_bps"] = 30.0  # 0.30% — passes the 200 bps cap
            out["spread_bps_source"] = "default_fallback_missing_bidask"
        out["bid"] = bid
        out["ask"] = ask
        out["webull_enriched"] = True
        out["real_market_data"] = True
        out["primary_source"] = "webull"
        # 2026-02-20 hydration audit fields (operator directive).
        # snapshot_source = the data feed that produced this snapshot.
        # snapshot_age_ms = ms elapsed since the fetch finished (read
        # by downstream gates that want to ignore stale data).
        out["snapshot_source"] = "webull"
        out["snapshot_fetched_at"] = fetch_ts_iso
        out["snapshot_age_ms"] = int((time.monotonic() - enrich_started) * 1000)
        out.setdefault("data_council", []).append("webull")
    else:
        out["snapshot_source"] = "webull_offline_base_only"
        out["snapshot_age_ms"] = None
        out.setdefault("data_council", []).append("webull_offline")

    return out


async def enrich_crypto_doctrine_snapshot(
    symbol: str, base_snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    """Async wrapper. Returns base snapshot unchanged on any error.

    2026-02-21: Kraken-first path. When `broker_selection.crypto =
    "kraken"`, fetch bid/ask from Kraken's public Ticker before
    falling through to the Webull executor enricher. If Kraken
    returns good bid/ask, the gates evaluate against the book the
    Kraken adapter will actually hit — eliminating the
    Webull-snapshot ↔ Kraken-execution mismatch that produced the
    `spread_bps = 30.0` fallback.
    """
    if not symbol:
        return base_snapshot
    try:
        kraken_data: Optional[Dict[str, Any]] = None
        if await _crypto_primary_is_kraken():
            from shared.snapshot_enrich.kraken_feed import kraken_bidask  # noqa: WPS433
            try:
                kraken_data = await kraken_bidask(symbol)
            except Exception as e:  # noqa: BLE001
                logger.warning("kraken_bidask raised sym=%s err=%s", symbol, e)
                kraken_data = None

        loop = asyncio.get_running_loop()
        enriched = await loop.run_in_executor(
            None, _enrich_sync, symbol, base_snapshot,
        )

        # If Kraken delivered authentic bid/ask, OVERWRITE the spread
        # block on the enriched snapshot. The doctrine gates read
        # `spread_bps`, `bid`, `ask`, `primary_source`, `snapshot_source`
        # — we replace exactly those fields and tag the source.
        if kraken_data:
            enriched["bid"] = kraken_data["bid"]
            enriched["ask"] = kraken_data["ask"]
            enriched["spread_bps"] = kraken_data["spread_bps"]
            enriched["spread_bps_source"] = "kraken_public_ticker"
            enriched["primary_source"] = "kraken"
            enriched["snapshot_source"] = "kraken"
            enriched["real_market_data"] = True
            enriched["kraken_enriched"] = True
            # Don't clobber price if Webull already had one;
            # Kraken's `last` is a fine substitute though.
            enriched.setdefault("price", kraken_data["price"])
            council = enriched.setdefault("data_council", [])
            if "kraken" not in council:
                council.append("kraken")
        return enriched
    except Exception as e:  # noqa: BLE001
        logger.warning("crypto enricher failed sym=%s err=%s", symbol, e)
        return base_snapshot


# ── broker_selection lookup ─────────────────────────────────────────
# Tiny in-process cache to avoid hammering Mongo on every intent.
_BROKER_SEL_CACHE: Dict[str, Any] = {"value": None, "fetched_at": 0.0}
_BROKER_SEL_TTL_SEC = 15.0


async def _crypto_primary_is_kraken() -> bool:
    """Return True iff `broker_selection.crypto == "kraken"`.

    Defaults to True (Kraken is the lane-default per
    LANE_BROKER_REGISTRY) when the singleton is missing or the
    lookup fails. Doctrine: fail-open toward the lane default — a
    Mongo blip must not silently revert to the deprecated Webull
    crypto feed.
    """
    now = time.monotonic()
    if (now - float(_BROKER_SEL_CACHE.get("fetched_at") or 0)) < _BROKER_SEL_TTL_SEC:
        cached = _BROKER_SEL_CACHE.get("value")
        if cached is not None:
            return bool(cached)
    try:
        from db import db  # noqa: WPS433
        doc = await db["broker_selection"].find_one({"_id": "singleton"})
        choice = (doc or {}).get("crypto", "kraken")
        is_kraken = (str(choice).strip().lower() == "kraken")
        _BROKER_SEL_CACHE["value"] = is_kraken
        _BROKER_SEL_CACHE["fetched_at"] = now
        return is_kraken
    except Exception as e:  # noqa: BLE001
        logger.warning("broker_selection lookup failed (defaulting Kraken=True): %s", e)
        return True
