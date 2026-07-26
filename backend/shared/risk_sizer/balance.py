"""Live balance providers — fetched before EVERY new entry.

Rules (operator spec): live fetch with 3s timeout; cached value no
older than 60s as fallback; neither → NO_TRADE (fail-closed). Never
inferred from execution receipts. Cache-sourced sizing is
conservative: effective equity = min(cached equity, cached available
+ marked open positions).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

logger = logging.getLogger("risedual.risk_sizer.balance")

_cache: dict[str, dict[str, Any]] = {}   # lane → {equity, available, at}


def reset_for_tests() -> None:
    _cache.clear()


def _open_positions_mark(lane: str) -> float:
    """Entry-notional proxy for open-position value (no live marks in
    the sizing path — conservative for longs near entry)."""
    from shared.hotpath import exit_plans  # noqa: WPS433
    total = 0.0
    for p in exit_plans.load_live(lane):
        total += float(p.get("entry_price") or 0.0) * float(p.get("qty_held") or 0.0)
    return total


async def _fetch_crypto_live() -> Optional[dict]:
    """Kraken Balance (available quote) + TradeBalance (equity)."""
    from shared.crypto.kraken import call_private, get_active_keys  # noqa: WPS433
    keys = await get_active_keys()
    if not keys:
        raise RuntimeError("no active Kraken keys")
    pub, priv = keys
    bal = await call_private("/0/private/Balance", pub, priv, {})
    available = 0.0
    for asset, qty in (bal or {}).items():
        if asset.upper() in ("ZUSD", "USD"):
            available += float(qty)
    tb = await call_private("/0/private/TradeBalance", pub, priv, {"asset": "ZUSD"})
    equity = float((tb or {}).get("eb") or (tb or {}).get("e") or 0.0)
    if equity <= 0:
        equity = available + _open_positions_mark("crypto")
    return {"equity": equity, "available": available}


async def _fetch_equity_live() -> Optional[dict]:
    """Webull account — equity and options lanes share the account.
    Cash accounts spend settled cash: coalesce buying_power → cash."""
    from shared.broker.webull import get_webull_adapter  # noqa: WPS433
    adapter = await get_webull_adapter()
    if adapter is None:
        raise RuntimeError("no Webull adapter (credentials missing)")
    acct = await adapter.get_account()
    equity = float(acct.get("equity") or 0.0)
    available = float(acct.get("buying_power") or acct.get("cash") or 0.0)
    return {"equity": equity, "available": available}


_FETCHERS = {
    "crypto": _fetch_crypto_live,
    "equity": _fetch_equity_live,
    "options": _fetch_equity_live,
}


async def get_balance_snapshot(lane: str, *, timeout_s: float = 3.0,
                               cache_max_age_s: float = 60.0) -> Optional[dict]:
    """Returns {equity, available, source: LIVE|CACHE, age_ms} or None
    (→ NO_TRADE)."""
    fetch = _FETCHERS.get(lane)
    if fetch is None:
        return None
    now = time.monotonic()
    try:
        live = await asyncio.wait_for(fetch(), timeout=timeout_s)
        if live and live["equity"] > 0:
            _cache[lane] = {**live, "at": now}
            return {**live, "source": "LIVE", "age_ms": 0}
    except Exception as exc:  # noqa: BLE001
        logger.warning("live %s balance fetch failed: %s", lane, exc)
    cached = _cache.get(lane)
    if cached and (now - cached["at"]) <= cache_max_age_s:
        age_ms = int((now - cached["at"]) * 1000)
        # Conservative equity when sizing from cache.
        equity = min(
            float(cached["equity"]),
            float(cached["available"]) + _open_positions_mark(lane),
        )
        return {"equity": equity, "available": float(cached["available"]),
                "source": "CACHE", "age_ms": age_ms}
    return None
