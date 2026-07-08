"""Shared price fetcher for the witness W/L resolver.

Extracted from `routes/admin_external_signals.py::resolve_witnesses` so
the admin-triggered pass and the scheduled background runner use ONE
price-lookup path. That guarantees the trigger endpoint's "dry run"
diagnostics reflect exactly what the runner will do when it fires.

Doctrine:
    The resolver's job is to compare the price at witness emission
    time to the price `horizon_hours` later. Both calls arrive
    here; we return the close of the last bar whose `ts` <= target.
    Broker-primary bars are always preferred (webull for equity,
    kraken_pro for crypto); polygon/finnhub are consulted only when
    the broker has no bars on file, per `bar_source.SOURCE_PRIORITY`.

    Returns None when no bar is on file for the target timestamp —
    the resolver then counts the row as `skipped_price_missing`,
    not misclassified.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from shared.research.bar_source import DEFAULT_TF_BY_LANE, load_recent_bars


async def price_from_ohlcv_bars(symbol: str, ts_iso: str) -> Optional[float]:
    """Look up the close price for `symbol` at-or-before `ts_iso`.

    Async so the resolver can `await` it. Pure I/O — no side effects,
    no writes. Callable signature matches `witness_resolver.PriceFetcher`.
    """
    # Lane inference: crypto pairs carry a `/USD` (or `/USDT`, etc.).
    # Equity tickers are alphanumeric without a slash.
    lane = "crypto" if "/" in symbol else "equity"
    tf = DEFAULT_TF_BY_LANE.get(lane, "1d")

    try:
        target = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None

    # limit=200: 1d timeframe covers ~4 months back, 1h covers 5 days —
    # enough for any horizon the resolver accepts (max 168h = 7 days).
    bars, _src = await load_recent_bars(symbol, tf=tf, limit=200)
    if not bars:
        return None

    best_price: Optional[float] = None
    for bar in bars:  # bars come oldest → newest
        try:
            bar_ts = datetime.fromisoformat(
                str(bar.get("ts")).replace("Z", "+00:00"),
            )
        except (TypeError, ValueError):
            continue
        if bar_ts <= target:
            best_price = float(bar.get("c") or 0.0) or None
        else:
            break
    return best_price
