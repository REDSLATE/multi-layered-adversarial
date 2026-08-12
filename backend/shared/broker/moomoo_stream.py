"""MooMoo OpenD live tick / order-book streaming (push, not poll).

Wired for the moment OpenD comes online: the worker loop probes the
configured gateway every RECONNECT_INTERVAL_S; while unreachable it
stays idle (status "waiting_for_opend") and every consumer falls back
gracefully — depth confirmation returns the NEUTRAL 0.50 sentinel and
the spread enrichment ladder simply skips this rung.

Consumers (all local cache reads, zero network in hot path):
    get_live_quote(symbol)   → {bid, ask, spread_bps, ts} | None
    get_depth_context(symbol)→ {confirmation 0..1, imbalance, depth}
                               fallback confirmation=0.50 when depth
                               is unavailable or stale.

Doctrine: Alpha never calls MooMoo directly — MC caches, brains read
via snapshot/spread enrichment. OpenD stays a user-hosted sidecar.

Configuration (backend/.env):
    MOOMOO_STREAM_ENABLED         default true (idle no-op when
                                  OpenD is unconfigured)
    MOOMOO_STREAM_MAX_SYMBOLS     default 30
    MOOMOO_STREAM_QUOTE_MAX_AGE_S default 10
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("risedual.moomoo_stream")

RECONNECT_INTERVAL_S = 60.0
UNIVERSE_REFRESH_S = 300.0
DEPTH_FALLBACK_CONFIRMATION = 0.50

_quotes: dict[str, dict[str, Any]] = {}
_books: dict[str, dict[str, Any]] = {}
_state: dict[str, Any] = {
    "running": False, "connected": False, "task": None, "ctx": None,
    "subscribed": [], "last_event_ts": None, "last_error": None,
    "connect_attempts": 0, "quote_events": 0, "book_events": 0,
}


def _max_age() -> float:
    return float(os.environ.get("MOOMOO_STREAM_QUOTE_MAX_AGE_S") or 10)


def _fresh(entry: Optional[dict], max_age_s: Optional[float] = None) -> bool:
    if not entry:
        return False
    return time.time() - entry.get("_mono", 0) <= (max_age_s or _max_age())


def get_live_quote(symbol: str, max_age_s: Optional[float] = None) -> Optional[dict]:
    """Fresh streamed top-of-book for an equity symbol, else None."""
    entry = _quotes.get(symbol.upper())
    if not _fresh(entry, max_age_s):
        return None
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def get_depth_context(symbol: str) -> dict[str, Any]:
    """Order-book confirmation context. Fallback is the NEUTRAL 0.50
    (operator-pinned) when depth is unavailable or stale — never a
    reason to block an entry."""
    entry = _books.get(symbol.upper())
    if not _fresh(entry):
        return {"confirmation": DEPTH_FALLBACK_CONFIRMATION,
                "source": "fallback_no_depth", "symbol": symbol.upper()}
    return {"confirmation": entry["confirmation"],
            "imbalance": entry["imbalance"],
            "bid_depth": entry["bid_depth"], "ask_depth": entry["ask_depth"],
            "ts": entry["ts"], "source": "moomoo_stream",
            "symbol": symbol.upper()}


def stream_status() -> dict[str, Any]:
    return {k: v for k, v in _state.items() if k not in ("task", "ctx")} | {
        "cached_quotes": len(_quotes), "cached_books": len(_books)}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _on_book(code: str, bids: list, asks: list) -> None:
    sym = code.split(".")[-1].upper()
    top_bid = float(bids[0][0]) if bids else None
    top_ask = float(asks[0][0]) if asks else None
    bid_depth = sum(float(b[1]) for b in bids[:5]) if bids else 0.0
    ask_depth = sum(float(a[1]) for a in asks[:5]) if asks else 0.0
    total = bid_depth + ask_depth
    imbalance = ((bid_depth - ask_depth) / total) if total > 0 else 0.0
    now = _now_iso()
    _books[sym] = {"bid_depth": round(bid_depth, 2),
                   "ask_depth": round(ask_depth, 2),
                   "imbalance": round(imbalance, 4),
                   "confirmation": round(0.5 + imbalance / 2.0, 4),
                   "ts": now, "_mono": time.time()}
    if top_bid and top_ask and top_ask > 0:
        mid = (top_bid + top_ask) / 2.0
        _quotes[sym] = {"bid": top_bid, "ask": top_ask,
                        "spread_bps": round((top_ask - top_bid) / mid * 1e4, 2),
                        "ts": now, "_mono": time.time()}
    _state["last_event_ts"] = now
    _state["book_events"] += 1


def _on_quote(code: str, last_price: Optional[float]) -> None:
    sym = code.split(".")[-1].upper()
    entry = _quotes.get(sym) or {}
    if last_price:
        entry["last"] = float(last_price)
        entry.setdefault("ts", _now_iso())
        entry["_mono"] = time.time()
        _quotes[sym] = entry
    _state["last_event_ts"] = _now_iso()
    _state["quote_events"] += 1


def _connect_sync(cfg: dict, symbols: list[str]):
    """Blocking connect + subscribe. Runs in a thread."""
    import tempfile

    from moomoo import (  # noqa: WPS433
        OpenQuoteContext, OrderBookHandlerBase, RET_OK,
        StockQuoteHandlerBase, SubType, SysConfig,
    )

    if cfg.get("rsa_pem"):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
        f.write(cfg["rsa_pem"])
        f.flush()
        os.chmod(f.name, 0o600)
        f.close()
        SysConfig.enable_proto_encrypt(True)
        SysConfig.set_init_rsa_file(f.name)

    class _QH(StockQuoteHandlerBase):
        def on_recv_rsp(self, rsp_pb):
            ret, data = super().on_recv_rsp(rsp_pb)
            if ret == RET_OK:
                try:
                    for row in data.to_dict("records"):
                        _on_quote(str(row.get("code") or ""),
                                  row.get("last_price"))
                except Exception:  # noqa: BLE001
                    pass
            return ret, data

    class _OBH(OrderBookHandlerBase):
        def on_recv_rsp(self, rsp_pb):
            ret, data = super().on_recv_rsp(rsp_pb)
            if ret == RET_OK:
                try:
                    _on_book(str(data.get("code") or ""),
                             data.get("Bid") or [], data.get("Ask") or [])
                except Exception:  # noqa: BLE001
                    pass
            return ret, data

    ctx = OpenQuoteContext(host=cfg["host"], port=cfg["port"],
                           is_encrypt=bool(cfg.get("rsa_pem")))
    ctx.set_handler(_QH())
    ctx.set_handler(_OBH())
    if symbols:
        codes = [f"US.{s}" for s in symbols]
        ret, msg = ctx.subscribe(codes, [SubType.QUOTE, SubType.ORDER_BOOK])
        if ret != RET_OK:
            ctx.close()
            raise RuntimeError(f"subscribe failed: {str(msg)[:200]}")
    return ctx


def _close_ctx() -> None:
    ctx = _state.get("ctx")
    if ctx is not None:
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
    _state["ctx"] = None
    _state["connected"] = False


async def _discover_symbols() -> list[str]:
    cap = int(os.environ.get("MOOMOO_STREAM_MAX_SYMBOLS") or 30)
    override = os.environ.get("MOOMOO_STREAM_SYMBOLS")
    if override:
        return [s.strip().upper() for s in override.split(",") if s.strip()][:cap]
    try:
        from datetime import timedelta

        from db import db  # noqa: WPS433
        cut = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        syms = await db["shared_intents"].distinct(
            "symbol", {"lane": "equity", "ingest_ts": {"$gte": cut}})
        return sorted({str(s).upper() for s in syms if s})[:cap]
    except Exception:  # noqa: BLE001
        return []


async def subscribe_symbols(symbols: list[str]) -> dict:
    """Manual/on-demand subscription (admin route)."""
    ctx = _state.get("ctx")
    if not _state["connected"] or ctx is None:
        return {"ok": False, "error": "not_connected"}

    def _do():
        from moomoo import RET_OK, SubType  # noqa: WPS433
        codes = [f"US.{s.upper()}" for s in symbols]
        ret, msg = ctx.subscribe(codes, [SubType.QUOTE, SubType.ORDER_BOOK])
        if ret != RET_OK:
            raise RuntimeError(str(msg)[:200])
    try:
        await asyncio.to_thread(_do)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    merged = sorted(set(_state["subscribed"]) | {s.upper() for s in symbols})
    _state["subscribed"] = merged
    return {"ok": True, "subscribed": merged}


async def _worker_loop() -> None:
    last_universe_sync = 0.0
    while _state["running"]:
        try:
            from shared.broker.moomoo_adapter import moomoo_config  # noqa: WPS433
            cfg = moomoo_config()
            if cfg is None:
                _state["last_error"] = "waiting_for_opend_config"
            elif not _state["connected"]:
                _state["connect_attempts"] += 1
                symbols = await _discover_symbols()
                try:
                    ctx = await asyncio.to_thread(_connect_sync, cfg, symbols)
                    _state.update({"ctx": ctx, "connected": True,
                                   "subscribed": symbols, "last_error": None})
                    last_universe_sync = time.time()
                    logger.info("moomoo stream connected, %s symbols", len(symbols))
                except Exception as exc:  # noqa: BLE001
                    _state["last_error"] = f"opend_unreachable: {str(exc)[:150]}"
            elif time.time() - last_universe_sync >= UNIVERSE_REFRESH_S:
                fresh = await _discover_symbols()
                new = sorted(set(fresh) - set(_state["subscribed"]))
                if new:
                    await subscribe_symbols(new)
                last_universe_sync = time.time()
        except Exception as exc:  # noqa: BLE001
            _state["last_error"] = str(exc)[:200]
            _close_ctx()
        try:
            await asyncio.sleep(RECONNECT_INTERVAL_S)
        except asyncio.CancelledError:
            break


def start_worker_if_enabled() -> None:
    if (os.environ.get("MOOMOO_STREAM_ENABLED") or "true").lower() != "true":
        logger.info("moomoo stream worker disabled via env")
        return
    if _state["running"]:
        return
    _state["running"] = True
    _state["task"] = asyncio.get_event_loop().create_task(_worker_loop())
    logger.info("moomoo stream worker started (idle until OpenD reachable)")


async def stop_worker() -> None:
    _state["running"] = False
    task = _state.get("task")
    if task:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _state["task"] = None
    _close_ctx()
