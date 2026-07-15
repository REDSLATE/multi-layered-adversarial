"""Webull equity movers — top gainers, losers, and most-active.

Wraps `webull.data.quotes.screener.get_gainers_losers` and
`get_most_active` behind the same `_guarded_call` + circuit breaker
the rest of `webull_quotes.py` uses. All calls SYNC; the refresher
dispatches through `asyncio.to_thread` so we don't block the event
loop.

Returns are normalized to:

    [{
      "canonical_symbol": "NVDA",
      "broker_instrument_id": "913255996",
      "change_pct": 4.83,
      "volume": 123456789.0,
      "price": 142.31,
      "source_reason": "top_gainer" | "top_loser" | "most_active",
    }, ...]

Fail-soft: any endpoint that errors returns `[]`. Refresher will
skip that source and log which one failed on the audit report.

2026-07-15 (iter-30 P4).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from shared.market_data.webull_quotes import _coerce_body, _guarded_call, get_quotes_client

logger = logging.getLogger("risedual.universe.webull_movers")


def _row_to_mover(row: dict, reason: str) -> Optional[dict]:
    """Normalize a screener row into the universe-symbol shape.
    Returns None on unparseable rows (drop silently)."""
    if not isinstance(row, dict):
        return None
    sym = (row.get("symbol") or row.get("ticker") or "").upper().strip()
    if not sym or not sym.isalpha() or len(sym) > 6:
        # Reject unparseable / non-standard-shaped tickers. The 6-
        # char cap keeps out class-share suffixes (BRK.A, etc.) that
        # Webull's SDK expresses inconsistently; those are safer to
        # add via operator pin than to auto-discover.
        return None
    try:
        change_pct = float(row.get("change_ratio") or row.get("changeRatio") or 0.0) * 100.0
    except (TypeError, ValueError):
        change_pct = 0.0
    try:
        volume = float(row.get("volume") or 0.0)
    except (TypeError, ValueError):
        volume = 0.0
    try:
        price = float(row.get("price") or row.get("close") or 0.0)
    except (TypeError, ValueError):
        price = 0.0
    iid = str(row.get("instrument_id") or row.get("instrumentId") or "")
    return {
        "canonical_symbol": sym,
        "broker_instrument_id": iid or None,
        "change_pct": round(change_pct, 4),
        "volume": volume,
        "price": price,
        "source_reason": reason,
    }


def _fetch_screener(
    client: Any, method_name: str, reason: str, cap: int, **kwargs,
) -> list[dict]:
    """Common shape: pull one screener endpoint, coerce to list of
    mover dicts. Guarded + fail-soft.

    `cap` is the max number of parsed rows to return (renamed from
    `page_size` to avoid a keyword collision with the SDK's own
    `page_size` argument, which the caller passes via `**kwargs`)."""
    fn = getattr(client._data.screener, method_name, None)
    if fn is None:
        logger.warning("webull screener missing method %s", method_name)
        return []
    r = _guarded_call(
        f"screener.{method_name}",
        lambda: fn(**kwargs),
    )
    if r is None:
        return []
    body = _coerce_body(r) or {}
    # Webull screener responses can arrive either as a list directly
    # or wrapped in `{"data": [...]}` — accept both.
    rows: list = []
    if isinstance(body, list):
        rows = body
    elif isinstance(body, dict):
        for k in ("data", "list", "items", "rows"):
            candidate = body.get(k)
            if isinstance(candidate, list):
                rows = candidate
                break
    out: list[dict] = []
    for row in rows[:cap]:
        mover = _row_to_mover(row, reason)
        if mover is not None:
            out.append(mover)
    return out


def fetch_top_gainers(page_size: int = 20) -> list[dict]:
    """DAY_1 gainers on US_STOCK, sorted by CHANGE_RATIO DESC."""
    client = get_quotes_client()
    if client is None:
        return []
    return _fetch_screener(
        client, "get_gainers_losers", "top_gainer", page_size,
        rank_type="DAY_1", category="US_STOCK",
        sort_by="CHANGE_RATIO", direction="DESC",
        page_size=str(page_size),
    )


def fetch_top_losers(page_size: int = 20) -> list[dict]:
    """DAY_1 losers on US_STOCK, sorted by CHANGE_RATIO ASC."""
    client = get_quotes_client()
    if client is None:
        return []
    return _fetch_screener(
        client, "get_gainers_losers", "top_loser", page_size,
        rank_type="DAY_1", category="US_STOCK",
        sort_by="CHANGE_RATIO", direction="ASC",
        page_size=str(page_size),
    )


def fetch_most_active(page_size: int = 20) -> list[dict]:
    """VOLUME-sorted most-active on US_STOCK."""
    client = get_quotes_client()
    if client is None:
        return []
    return _fetch_screener(
        client, "get_most_active", "most_active", page_size,
        category="US_STOCK",
        rank_type="VOLUME", sort_by="VOLUME", direction="DESC",
        page_size=str(page_size),
    )
