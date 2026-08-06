"""Kraken WebSocket v2 real-time layer (2026-08-05 operator directive:
"I want it to see the information in real-time so it can make
decisions right away" — the issue has been missing momentum moves).

One public WS connection (no auth) streaming ticker for the watch set
(pins + universe movers + ignition candidates, capped). Provides:
  · get_live_quote(sym)      — bid/ask/last, age <1s (REST ladder stays
                               as fallback; this is a speed layer)
  · current_partial_bar(sym) — the FORMING 1m bar, so momentum math
                               sees the move now, not after bar close
  · thrust trigger           — a tick moving ≥ thrust_bps from its
                               60s baseline marks the symbol HOT and
                               wakes the scanner immediately
Decision latency drops from ~2-3 min (bar close → feeder → next 60s
cycle) to seconds. Fail-soft everywhere: WS loss reverts the system
to the REST path it ran on before.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("risedual.kraken_ws")

WS_URL = "wss://ws.kraken.com/v2"
_BASELINE_MAX_AGE_S = 60.0
_RESUB_CHECK_S = 60.0

_quotes: dict[str, dict] = {}
_partial: dict[str, dict] = {}
_thrust_base: dict[str, tuple[float, float]] = {}
_hot: set[str] = set()
_hot_event: Optional[asyncio.Event] = None
_status: dict[str, Any] = {"connected": False, "symbols": 0, "ticks": 0,
                           "hot_triggers": 0, "last_msg_at": None,
                           "reconnects": 0}


def hot_event() -> asyncio.Event:
    global _hot_event  # noqa: PLW0603
    if _hot_event is None:
        _hot_event = asyncio.Event()
    return _hot_event


def reset_for_tests() -> None:
    _quotes.clear()
    _partial.clear()
    _thrust_base.clear()
    _hot.clear()
    _status.update(connected=False, symbols=0, ticks=0, hot_triggers=0,
                   last_msg_at=None, reconnects=0)


def status() -> dict:
    return dict(_status)


def get_live_quote(symbol: str, max_age_s: float = 3.0) -> Optional[dict]:
    q = _quotes.get(symbol)
    if not q:
        return None
    age_s = time.monotonic() - q["mono"]
    if age_s > max_age_s:
        return None
    return {"bid": q["bid"], "ask": q["ask"], "last": q["last"],
            "volume_24h_usd": q.get("volume_24h_usd"),
            "age_ms": round(age_s * 1000.0, 1)}


def current_partial_bar(symbol: str) -> Optional[dict]:
    pb = _partial.get(symbol)
    if not pb:
        return None
    if time.monotonic() - pb["mono"] > 120:
        return None  # stream stopped ticking this symbol
    return {"ts": pb["ts"], "o": pb["o"], "h": pb["h"], "l": pb["l"],
            "c": pb["c"], "v": pb["v"], "partial": True}


def take_hot_symbols() -> set[str]:
    hot = set(_hot)
    _hot.clear()
    return hot


def on_tick(symbol: str, bid: float, ask: float, last: float,
            volume_24h_usd: Optional[float], thrust_bps: float,
            now_mono: Optional[float] = None,
            now_dt: Optional[datetime] = None) -> bool:
    """Update quote + forming 1m bar; True when the thrust trigger
    fires (≥ thrust_bps move from the ≤60s baseline)."""
    now_mono = now_mono if now_mono is not None else time.monotonic()
    now_dt = now_dt or datetime.now(timezone.utc)
    prev = _quotes.get(symbol) or {}
    _quotes[symbol] = {
        "bid": bid or prev.get("bid") or 0.0,
        "ask": ask or prev.get("ask") or 0.0,
        "last": last or prev.get("last") or 0.0,
        "volume_24h_usd": (volume_24h_usd
                           if volume_24h_usd is not None
                           else prev.get("volume_24h_usd")),
        "mono": now_mono,
    }
    if last and last > 0:
        minute = now_dt.replace(second=0, microsecond=0).isoformat()
        pb = _partial.get(symbol)
        if not pb or pb["ts"] != minute:
            _partial[symbol] = {"ts": minute, "o": last, "h": last,
                                "l": last, "c": last, "v": 0.0,
                                "mono": now_mono}
        else:
            pb["h"] = max(pb["h"], last)
            pb["l"] = min(pb["l"], last)
            pb["c"] = last
            pb["mono"] = now_mono
        base = _thrust_base.get(symbol)
        if not base or now_mono - base[0] > _BASELINE_MAX_AGE_S:
            _thrust_base[symbol] = (now_mono, last)
        elif base[1] > 0 and abs(last / base[1] - 1.0) * 10_000 >= thrust_bps:
            _thrust_base[symbol] = (now_mono, last)
            _hot.add(symbol)
            _status["hot_triggers"] += 1
            return True
    return False


async def _watch_set(max_symbols: int) -> list[str]:
    syms: list[str] = []
    try:
        from momentum.momentum_scanner import STATE_ID, _lane_symbols  # noqa: WPS433
        syms = list(await _lane_symbols("crypto"))
        from db import db  # noqa: WPS433
        st = await db["runtime_flags"].find_one(
            {"_id": STATE_ID}, {"ignition": 1}, max_time_ms=3000) or {}
        for c in (st.get("ignition") or []):
            if c.get("symbol") and c["symbol"] not in syms:
                syms.append(c["symbol"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("kraken_ws watch-set build failed: %s", exc)
    return syms[:max_symbols]


async def _cfg() -> dict:
    from momentum.momentum_scanner import get_config  # noqa: WPS433
    return await get_config()


async def stream_loop() -> None:  # pragma: no cover — network loop
    import websockets  # noqa: WPS433
    logger.info("kraken_ws real-time layer started")
    while True:
        try:
            cfg = await _cfg()
            if not cfg.get("realtime_enabled", True):
                _status["connected"] = False
                await asyncio.sleep(30)
                continue
            thrust_bps = float(cfg.get("thrust_bps") or 50)
            max_syms = int(cfg.get("max_ws_symbols") or 60)
            watch = await _watch_set(max_syms)
            if not watch:
                await asyncio.sleep(30)
                continue
            async with websockets.connect(WS_URL, ping_interval=20,
                                          ping_timeout=20) as ws:
                await ws.send(json.dumps({
                    "method": "subscribe",
                    "params": {"channel": "ticker", "symbol": watch}}))
                _status.update(connected=True, symbols=len(watch))
                logger.info("kraken_ws connected: %d symbols", len(watch))
                last_resub = time.monotonic()
                subscribed = set(watch)
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    except asyncio.TimeoutError:
                        raw = None
                    if raw:
                        msg = json.loads(raw)
                        if msg.get("channel") == "ticker":
                            for d in (msg.get("data") or []):
                                vol = None
                                try:
                                    v = float(d.get("volume") or 0)
                                    vw = float(d.get("vwap") or 0)
                                    vol = round(v * vw, 2) if v and vw else None
                                except (TypeError, ValueError):
                                    pass
                                fired = on_tick(
                                    d.get("symbol") or "",
                                    float(d.get("bid") or 0),
                                    float(d.get("ask") or 0),
                                    float(d.get("last") or 0),
                                    vol, thrust_bps)
                                if fired:
                                    hot_event().set()
                            _status["ticks"] += len(msg.get("data") or [])
                            _status["last_msg_at"] = datetime.now(
                                timezone.utc).isoformat()
                    if time.monotonic() - last_resub > _RESUB_CHECK_S:
                        last_resub = time.monotonic()
                        cfg = await _cfg()
                        if not cfg.get("realtime_enabled", True):
                            break
                        thrust_bps = float(cfg.get("thrust_bps") or 50)
                        desired = set(await _watch_set(max_syms))
                        add = sorted(desired - subscribed)
                        drop = sorted(subscribed - desired)
                        if add:
                            await ws.send(json.dumps({
                                "method": "subscribe",
                                "params": {"channel": "ticker",
                                           "symbol": add}}))
                        if drop:
                            await ws.send(json.dumps({
                                "method": "unsubscribe",
                                "params": {"channel": "ticker",
                                           "symbol": drop}}))
                        subscribed = desired or subscribed
                        _status["symbols"] = len(subscribed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _status["connected"] = False
            _status["reconnects"] += 1
            logger.warning("kraken_ws stream error (reconnect in 5s): %s",
                           exc)
            await asyncio.sleep(5)
