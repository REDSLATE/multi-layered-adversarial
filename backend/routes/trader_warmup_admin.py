"""Trader warmup progress endpoint (2026-07-03).

Per-symbol OHLCV bar counts against the research-layer warmup floor.
Answers the operator question "why isn't NVDA firing yet?" during the
first ~50 minutes after a redeploy — the research pipeline needs 50
bars per symbol before its strategy signals stop returning `warmup`.

Deliberately SEPARATE from `/api/admin/trader/status`:
    * `/status` promises to serve even when Atlas is unreachable
      (reads only local SQLite + in-memory state).
    * This endpoint DOES read Mongo for bar counts. Wrapped in
      `asyncio.wait_for` with graceful degrade — same soft-error
      contract as `/parabolic-phase/phases`.

Doctrine (locked 2026-07-03):
    Any new endpoint that hits Atlas must either (a) tolerate slow/dead
    Atlas gracefully with a soft-error envelope, or (b) live on a page
    the operator can tolerate not seeing during Atlas incidents.
    Never let a dashboard tile freeze the whole app.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List

from fastapi import APIRouter, Depends

from auth import get_current_user
from db import db

logger = logging.getLogger("risedual.trader_warmup")
router = APIRouter(prefix="/admin/trader", tags=["trader-warmup"])

_MONGO_READ_TIMEOUT_S = 8.0
# The research layer's warmup floor. Keep in sync with
# `backend/shared/research/backtest.py::_MIN_WARMUP_BARS`.
_MIN_WARMUP_BARS = 50


def _configured_universe() -> List[Dict[str, str]]:
    """Return a list of {symbol, lane, tf} entries for every symbol
    the trader is configured to watch, per the current env vars.
    Uses the plural helpers (fall back to singular for backward
    compat) so this endpoint works pre- and post-multi-ticker deploy.
    """
    def _split(env_key: str) -> List[str]:
        raw = (os.environ.get(env_key) or "").strip()
        if not raw:
            return []
        return [x.strip().upper() for x in raw.split(",") if x.strip()]

    equity = _split("TRADER_EQUITY_TICKERS") or (
        [os.environ.get("TRADER_EQUITY_TICKER", "TSLA").upper()]
    )
    crypto = _split("TRADER_CRYPTO_PAIRS") or (
        [os.environ.get("TRADER_CRYPTO_PAIR", "XBTUSD").upper()]
    )
    out = []
    for s in equity:
        out.append({"symbol": s, "lane": "equity", "tf": "1d"})
    for s in crypto:
        out.append({"symbol": s, "lane": "crypto", "tf": "1h"})
    return out


async def _count_bars(symbol: str, tf: str) -> int:
    """Count OHLCV bars available for one symbol/tf across ALL sources.

    Uses the same `shared_ohlcv_bars` collection the research pipeline
    reads from. If Atlas is slow, `asyncio.wait_for` at the caller
    surfaces the timeout as a soft-error rather than hanging the tile.
    """
    return await db["shared_ohlcv_bars"].count_documents({
        "symbol": symbol, "tf": tf,
    })


@router.get("/warmup-progress")
async def warmup_progress(_: dict = Depends(get_current_user)) -> Dict[str, Any]:
    """Per-symbol warmup progress across the configured universe.

    Returns a `symbols` list with each entry showing:
        symbol, lane, tf, bars, required, ready (bool), pct_complete

    Plus an aggregate `all_ready` boolean so the UI can render a
    single "brains ready" badge without iterating.

    Soft-degrades on Atlas timeout / read error — same envelope shape
    as `/parabolic-phase/phases`. Operator gets a paused-tape note
    instead of a raw exception.
    """
    universe = _configured_universe()
    if not universe:
        return {
            "ok": True, "symbols": [], "all_ready": True,
            "min_warmup_bars": _MIN_WARMUP_BARS,
            "note": "no symbols configured — trader universe is empty",
        }

    try:
        counts = await asyncio.wait_for(
            asyncio.gather(*[_count_bars(u["symbol"], u["tf"]) for u in universe]),
            timeout=_MONGO_READ_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "warmup_progress: Atlas read timed out after %.1fs",
            _MONGO_READ_TIMEOUT_S,
        )
        return {
            "ok": True, "symbols": [], "all_ready": False,
            "min_warmup_bars": _MIN_WARMUP_BARS,
            "error": "mongo_timeout",
            "message": (
                f"MongoDB Atlas read timed out after {_MONGO_READ_TIMEOUT_S:.0f}s. "
                f"Warmup progress paused; retry when Atlas recovers."
            ),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "warmup_progress: %s: %s",
            type(exc).__name__, str(exc)[:200],
        )
        return {
            "ok": True, "symbols": [], "all_ready": False,
            "min_warmup_bars": _MIN_WARMUP_BARS,
            "error": "mongo_error",
            "message": f"{type(exc).__name__}: {exc}"[:240],
        }

    symbols_out: List[Dict[str, Any]] = []
    all_ready = True
    for u, bars in zip(universe, counts):
        ready = bars >= _MIN_WARMUP_BARS
        if not ready:
            all_ready = False
        pct = min(100, round(bars / _MIN_WARMUP_BARS * 100))
        symbols_out.append({
            "symbol": u["symbol"],
            "lane": u["lane"],
            "tf": u["tf"],
            "bars": bars,
            "required": _MIN_WARMUP_BARS,
            "ready": ready,
            "pct_complete": pct,
        })

    # Sort not-ready first so the operator sees the blockers up top.
    symbols_out.sort(key=lambda r: (r["ready"], r["symbol"]))

    return {
        "ok": True,
        "symbols": symbols_out,
        "all_ready": all_ready,
        "min_warmup_bars": _MIN_WARMUP_BARS,
        "total_symbols": len(symbols_out),
        "ready_count": sum(1 for r in symbols_out if r["ready"]),
    }
