"""Webull MQTT L1 quote stream — fluid-machine upgrade path.

Doctrine pin (2026-07-02):
    Same-broker doctrine: the equity lane already trades through Webull.
    This module opens a persistent MQTT subscription to Webull's
    `data-api.webull.com` gateway, receives protobuf-encoded QUOTE
    messages tick-by-tick (best bid/ask), decodes them via the
    official `webull-python-sdk-mdata` SDK, and updates the SAME
    in-memory cache the HTTP snapshot poller writes to
    (`trader.spread._latest`) plus the durable SQLite tape
    (`trader.store.record_spread_tick`, `source="webull_mqtt"`).

    Coexists with the HTTP poller by design — whichever source
    produces the newer tick wins for gate/dashboard reads. The
    poller is a safety net for MQTT dropouts.

    Runs on a dedicated background thread because paho-mqtt (which
    the SDK wraps) is thread-based, not asyncio. Cache + store
    writes are thread-safe (SQLite lock in store.py already, and
    dict assignment for _latest is atomic under GIL).

Requires the L1 market-data subscription active on the operator's
OpenAPI plan AND a valid streaming credential (the SDK trades
`app_key`+`app_secret` for a streaming token via gRPC). Failure
falls back gracefully — poller keeps running.
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


def _on_quote_message(client, userdata, message) -> None:
    """paho-mqtt on_message callback. The SDK's DefaultQuotesClient
    dispatches decoded messages here as `(client, userdata,
    QuoteResult|SnapshotResult|TickResult)`. We handle QuoteResult
    (best bid/ask) and ignore the rest — the HTTP poller already
    tracks OHLC via the snapshot endpoint."""
    try:
        # Late import so the module is importable without SDK deps
        # if someone runs the trader without streaming enabled.
        from webullsdkmdata.quotes.subscribe.quote_result import QuoteResult
    except Exception:  # noqa: BLE001
        return
    if not isinstance(message, QuoteResult):
        return
    try:
        basic = message.get_basic()
        symbol = (basic.get_symbol() or "").upper()
        if not symbol:
            return
        asks = message.get_asks() or []
        bids = message.get_bids() or []
        if not (asks and bids):
            return
        try:
            ask = float(asks[0].get_price())
            bid = float(bids[0].get_price())
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
        # Cache + persist. Thread-safety: store.record_spread_tick
        # holds its own lock; dict assignment on _latest is atomic
        # under CPython's GIL.
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


def _on_connect_success(client, userdata, session_id) -> None:
    logger.info("stream connected session=%s", session_id)
    _set_status(state="connected", started_at=_now_iso())


def _on_subscribe_success(client, grpc_client, token) -> None:
    logger.info("stream subscription confirmed")


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
    endpoint = config.equity_stream_endpoint() or None

    try:
        from webullsdkmdata.common.category import Category   # noqa: WPS433
        from webullsdkmdata.common.subscribe_type import SubscribeType  # noqa: WPS433
        from webullsdkmdata.quotes.subscribe.default_client import (  # noqa: WPS433
            DefaultQuotesClient,
        )
        # Turn on the SDK's own logging so an operator can see the
        # gRPC handshake + MQTT connect steps in the backend log
        # instead of guessing at what the stream is doing.
        import logging as _logging  # noqa: WPS433
        _logging.getLogger("webullsdkquotescore").setLevel(_logging.INFO)
        _logging.getLogger("webullsdkmdata").setLevel(_logging.INFO)
        _logging.getLogger("webullsdkcore").setLevel(_logging.INFO)
        # Enable paho-mqtt's own trace so a failed CONNECT / SUBACK
        # surfaces in the log instead of a silent hang.
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
            _client = DefaultQuotesClient(
                app_key, app_secret, region, endpoint,
            )
            _client.init_default_settings(
                symbols,
                Category.US_STOCK.name,
                SubscribeType.QUOTE.name,
            )
            _client.on_quotes_message = _on_quote_message
            _client.on_connect_success = _on_connect_success
            _client.on_subscribe_success = _on_subscribe_success
            logger.info(
                "stream starting symbols=%s region=%s endpoint=%s",
                symbols, region, endpoint or "<sdk-default>",
            )
            _client.connect_and_loop_forever()  # blocks
            # Reached only on clean disconnect
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
