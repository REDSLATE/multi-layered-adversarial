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
# deduper — kraken_pro for crypto, thinkorswim for equities.
SOURCE_PRIORITY = ["kraken_pro", "thinkorswim", "manual"]
VALID_TFS = frozenset({"1m", "5m", "15m", "1h", "4h", "1d"})


async def pick_source(symbol: str, tf: str) -> Optional[str]:
    """Best available bar source for (symbol, tf), or None if no bars
    are on file at all."""
    sources = await db[SHARED_OHLCV_BARS].distinct(
        "source", {"symbol": symbol, "tf": tf},
    )
    if not sources:
        return None
    for s in SOURCE_PRIORITY:
        if s in sources:
            return s
    return sources[0]


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
