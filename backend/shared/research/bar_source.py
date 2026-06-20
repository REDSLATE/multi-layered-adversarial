"""Bar source helper for the Research Layer.

Single async fetch path used by both the read-only HTTP endpoint
(`routes/research.py`) and the brain runtimes that want to stamp
research evidence onto outgoing intents (e.g. the GTO/redeye crypto
bridge). Keeps the Mongo collection name + source-priority logic in
one place.

Doctrine guard: this helper READS bars only. It exposes no writer,
no submit, no broker call.
"""
from __future__ import annotations

from typing import Optional

from db import db
from namespaces import SHARED_OHLCV_BARS


# Same preferred-source order as `/api/public/bars` and the heatmap
# deduper. Lane-aware: crypto bars come from kraken_pro by default,
# equity bars from polygon / finnhub. Falls through to any other
# source if the preferred one is absent for a (symbol, tf) pair.
SOURCE_PRIORITY = ["kraken_pro", "polygon", "finnhub_equity", "thinkorswim", "manual"]
VALID_TFS = frozenset({"1m", "5m", "15m", "1h", "4h", "1d"})

# Default timeframe per lane — crypto is intraday-active (1h) while
# equity bars in this codebase live at 1d granularity. Bridges and
# routes that don't override the `tf` argument fall back to this
# table.
DEFAULT_TF_BY_LANE = {
    "crypto": "1h",
    "equity": "1d",
}


async def pick_source(symbol: str, tf: str) -> Optional[str]:
    """Best available bar source for (symbol, tf), or None if no bars
    are on file at all.

    Selection rule: pick the source with the DEEPEST history. This
    matters because the same (symbol, tf) can land in multiple
    collections at very different depths — e.g. AAPL/1d has 9 bars
    via polygon-trial but 2500+ via finnhub_equity. The Strategy Lab
    needs warm SMA-50 / MACD-26 history to score; biasing toward
    bar count produces materially better evidence than biasing toward
    a named source.

    Tie-broken by `SOURCE_PRIORITY` order so behavior is deterministic
    when two sources hold the same depth.
    """
    pipeline = [
        {"$match": {"symbol": symbol, "tf": tf}},
        {"$group": {"_id": "$source", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]
    rows = await db[SHARED_OHLCV_BARS].aggregate(pipeline).to_list(None)
    if not rows:
        return None
    # Highest bar count first; tie-broken by SOURCE_PRIORITY index.
    def _key(r: dict) -> tuple:
        src = r["_id"]
        try:
            pri = SOURCE_PRIORITY.index(src)
        except ValueError:
            pri = len(SOURCE_PRIORITY)
        return (-int(r["n"]), pri)
    rows.sort(key=_key)
    return rows[0]["_id"]


async def load_recent_bars(
    symbol: str,
    tf: str = "1h",
    limit: int = 120,
    source: Optional[str] = None,
) -> tuple[list[dict], Optional[str]]:
    """Load up to `limit` most-recent OHLCV bars, returned oldest →
    newest. Returns ([], None) when no source is available — callers
    are expected to no-op rather than error on cold-start symbols.
    """
    src = source or await pick_source(symbol, tf)
    if not src:
        return [], None
    rows = await db[SHARED_OHLCV_BARS].find(
        {"symbol": symbol, "tf": tf, "source": src},
        {"_id": 0, "ts": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
    ).sort("ts", -1).to_list(limit)
    rows.reverse()
    return rows, src
