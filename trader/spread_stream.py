"""Webull MQTT L1 quote stream — fluid-machine upgrade path.

Doctrine pin (2026-07-02, revised after operator supplied the
canonical SDK pattern):
    Same-broker doctrine: the equity lane already trades through Webull.
    This module opens a persistent MQTT subscription to Webull's
    `data-api.webull.com` gateway using the newer umbrella SDK's
    `DataStreamingClient` (which decouples the gRPC token-exchange
    host from the MQTT gateway host — an earlier attempt with the
    legacy `DefaultQuotesClient` coupled them, causing `UNAVAILABLE:
    tcp handshaker shutdown` on the MQTT gateway).

    Receives QUOTE messages (best bid/ask), extracts L1, and updates
    the SAME in-memory cache the HTTP snapshot poller writes to
    (`trader.spread._latest`) plus the durable SQLite tape
    (`trader.store.record_spread_tick`, `source="webull_mqtt"`).

    Coexists with the HTTP poller by design — whichever source
    produces the newer tick wins for gate/dashboard reads. The
    poller is a safety net for MQTT dropouts.

    Runs on a dedicated background thread because paho-mqtt (which
    the SDK wraps) is thread-based, not asyncio. Cache + store
    writes are thread-safe (SQLite lock in store.py already, and
    dict assignment for _latest is atomic under GIL).

Session id: `mc_paradox_equity_1` by default (override with
`TRADER_EQUITY_STREAM_SESSION_ID`). Reusing an active session_id
boots the earlier connection; max 5 concurrent per App Key.

Requires the L1 market-data subscription active on the operator's
OpenAPI plan. Failure falls back gracefully — poller keeps running.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from trader import config, spread, store


logger = logging.getLogger("trader.spread_stream")

_thread: Optional[threading.Thread] = None
_client = None
_stop_flag = threading.Event()
_status: dict = {
    "state": "stopped",       # stopped | starting | connected | error
    "started_at": None,
    "last_message_at": None,
    "message_count": 0,
    "last_error": None,
    "subscribed_symbols": [],
}
_status_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_unix() -> float:
    return datetime.now(timezone.utc).timestamp()


def _set_status(**kwargs) -> None:
    with _status_lock:
        _status.update(kwargs)


def get_status() -> dict:
    with _status_lock:
        return dict(_status)


def _on_quote_message(client, topic, quotes) -> None:
    """DataStreamingClient dispatches decoded messages here as
    `(client, topic, quotes)`. The `quotes` payload is the SDK's
    decoded protobuf — QUOTE topics carry best bid/ask, SNAPSHOT
    carries OHLC, TICK carries trade prints. We only extract L1
    from QUOTE messages (matching what the risk gate needs)."""
    try:
        # QUOTE messages have `bidList` / `askList` on the top-level
        # payload; SNAPSHOT and TICK use different shapes. Duck-type
        # so unrelated topics silently no-op.
        if not hasattr(quotes, "get_asks") and not isinstance(quotes, dict):
            return
        if hasattr(quotes, "get_asks"):
            asks = quotes.get_asks() or []
            bids = quotes.get_bids() or []
            basic = quotes.get_basic() if hasattr(quotes, "get_basic") else None
            symbol = (basic.get_symbol() if basic else "").upper()
        else:
            # dict shape — best-effort field pluck
            asks = quotes.get("askList") or quotes.get("asks") or []
            bids = quotes.get("bidList") or quotes.get("bids") or []
            symbol = (quotes.get("symbol") or "").upper()
        if not symbol or not (asks and bids):
            return
        # Each entry is either an object with get_price() or a dict
        def _px(entry):
            if hasattr(entry, "get_price"):
                return entry.get_price()
            if isinstance(entry, dict):
                return entry.get("price") or entry.get("Price")
            return None
        try:
            ask = float(_px(asks[0]) or 0)
            bid = float(_px(bids[0]) or 0)
        except (TypeError, ValueError):
            return
        if ask <= 0 or bid <= 0 or ask < bid:
            return
        mid = (ask + bid) / 2.0
        spread_abs = ask - bid
        spread_bps = (spread_abs / mid) * 10_000 if mid > 0 else 0.0
        row = {
            "ts": _now_iso(),
            "pair": symbol,
            "lane": "equity",
            "bid": bid, "ask": ask, "last": None,
            "spread_abs": spread_abs,
            "spread_bps": round(spread_bps, 4),
            "source": "webull_mqtt",
        }
        spread._cache_row(row)
        try:
            store.record_spread_tick(row)
        except Exception as e:  # noqa: BLE001
            logger.error("stream store write failed symbol=%s err=%s",
                         symbol, e)
        with _status_lock:
            _status["last_message_at"] = _now_iso()
            _status["message_count"] += 1
    except Exception as e:  # noqa: BLE001
        logger.warning("stream message handling failed: %s", e)


def _on_connect_success(client, api_client, session_id) -> None:
    """DataStreamingClient signature: (client, api_client, session_id).
    Subscribes to the configured symbols on connect — the SDK doesn't
    auto-subscribe. Boot-time subscription is idempotent-per-session."""
    logger.info("stream connected session=%s", session_id)
    _set_status(state="connected", started_at=_now_iso())
    try:
        from webull.data.common.category import Category  # noqa: WPS433
        from webull.data.common.subscribe_type import SubscribeType  # noqa: WPS433
        symbols = list(config.equity_stream_symbols())
        sub_types = [
            getattr(SubscribeType, s).name
            for s in config.equity_stream_sub_types()
            if hasattr(SubscribeType, s)
        ]
        if not sub_types:
            sub_types = [SubscribeType.QUOTE.name]
        client.subscribe(symbols, Category.US_STOCK.name, sub_types)
        logger.info(
            "stream subscribing symbols=%s sub_types=%s",
            symbols, sub_types,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("stream subscribe on connect failed: %s", e)
        _set_status(state="error", last_error=f"subscribe: {e}")


def _on_subscribe_success(client, api_client, session_id) -> None:
    logger.info("stream subscription confirmed session=%s", session_id)
    _set_status(state="connected")


def _run_loop() -> None:
    """Background-thread entrypoint. Owns the SDK client lifecycle
    and reconnects on failure with exponential backoff."""
    creds = spread._webull_creds()
    if not creds:
        _set_status(state="error", last_error="creds_missing")
        logger.warning("stream stopped: WEBULL_APP_KEY/SECRET missing")
        return
    app_key, app_secret, _ = creds

    symbols = list(config.equity_stream_symbols())
    if not symbols:
        _set_status(state="error", last_error="no_symbols")
        return
    region = config.equity_stream_region()
    mqtt_host = config.equity_stream_endpoint() or None
    http_host = config.equity_stream_http_host() or None
    session_id = config.equity_stream_session_id()

    try:
        # Newer umbrella SDK (webull-openapi-python-sdk v2.x) — decouples
        # the gRPC (http_host) and MQTT (mqtt_host) endpoints. The older
        # `webullsdkmdata.DefaultQuotesClient` coupled them, causing
        # `UNAVAILABLE: tcp handshaker shutdown` because the MQTT gateway
        # doesn't accept gRPC on port 443.
        from webull.data.data_streaming_client import (  # noqa: WPS433
            DataStreamingClient,
        )
        import logging as _logging  # noqa: WPS433
        _logging.getLogger("webull").setLevel(_logging.INFO)
        _logging.getLogger("webullsdkquotescore").setLevel(_logging.INFO)
        _logging.getLogger("paho.mqtt.client").setLevel(_logging.INFO)
    except Exception as e:  # noqa: BLE001
        _set_status(state="error", last_error=f"sdk_import: {e}")
        logger.error("stream stopped: SDK import failed: %s", e)
        return

    backoff = 5
    while not _stop_flag.is_set():
        try:
            _set_status(
                state="starting",
                subscribed_symbols=symbols,
                last_error=None,
            )
            global _client
            _client = DataStreamingClient(
                app_key, app_secret, region, session_id,
                http_host=http_host, mqtt_host=mqtt_host,
            )
            _client.on_quotes_message = _on_quote_message
            _client.on_connect_success = _on_connect_success
            _client.on_subscribe_success = _on_subscribe_success
            logger.info(
                "stream starting symbols=%s region=%s http=%s mqtt=%s session=%s",
                symbols, region,
                http_host or "<sdk-default>",
                mqtt_host or "<sdk-default>",
                session_id,
            )
            _client.connect_and_loop_forever()  # blocks
            if _stop_flag.is_set():
                logger.info("stream loop exited (stop requested)")
                break
            logger.warning(
                "stream loop returned unexpectedly; reconnecting in %ds",
                backoff,
            )
        except Exception as e:  # noqa: BLE001
            _set_status(state="error", last_error=str(e))
            logger.warning("stream error: %s (reconnecting in %ds)", e, backoff)
        if _stop_flag.wait(backoff):
            break
        backoff = min(backoff * 2, 120)
    _set_status(state="stopped")


def start() -> None:
    """Launch the streaming background thread. Idempotent."""
    global _thread
    if not config.equity_stream_enabled():
        logger.info("stream DISABLED (TRADER_EQUITY_STREAM_ENABLED=false)")
        return
    if _thread and _thread.is_alive():
        logger.info("stream already running (thread alive)")
        return
    _stop_flag.clear()
    _thread = threading.Thread(
        target=_run_loop, name="trader.spread_stream", daemon=True,
    )
    _thread.start()


def stop(timeout: float = 5.0) -> None:
    """Signal the streaming thread to exit and join it."""
    _stop_flag.set()
    global _client
    try:
        if _client and hasattr(_client, "disconnect"):
            _client.disconnect()
    except Exception:  # noqa: BLE001
        pass
    if _thread:
        _thread.join(timeout=timeout)
    _set_status(state="stopped")
