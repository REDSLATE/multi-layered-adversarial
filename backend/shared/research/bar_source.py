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


# Bar source priority — operator doctrine: the broker is the source
# of truth for what we'd actually trade against. Polygon/finnhub are
# BACKUPS, only consulted when the broker has no bars for that
# (symbol, tf) on file. Operator pinned this 2026-02-20:
#     "the primary sources should be the broker themselves.
#      Polygon and finnhub should be back up."
#
# Order: webull (equity broker) and kraken_pro (crypto broker) first,
# then the alt-data backups, then last-resort manual ingest.
SOURCE_PRIORITY = [
    "webull",           # equity broker (primary, when bars start flowing)
    "webull_equity",    # in case the ingest channel uses the longer name
    "kraken_pro",       # crypto broker (primary)
    "kraken",           # alt kraken channel name (defensive)
    "polygon",          # equity backup
    "finnhub_equity",   # equity backup-of-backup
    "thinkorswim",      # legacy equity backup
    "manual",           # operator-inserted, last resort
]
VALID_TFS = frozenset({"1m", "5m", "15m", "1h", "4h", "1d"})

# Default timeframe per lane — crypto is intraday-active (1h) while
# equity bars in this codebase live at 1d granularity. Bridges and
# routes that don't override the `tf` argument fall back to this
# table.
DEFAULT_TF_BY_LANE = {
    "crypto": "1h",
    "equity": "1d",
}


# When a source has fewer than this many bars on file for a given
# (symbol, tf), treat it as "shallow" — fine for the broker (we
# always trust the broker), but for backup sources we'll skip past
# them in favor of the next backup with deeper history. Tuned to
# 50 because that's the warmup floor for SMA-50 / MACD-26 inside
# `large_cap_momentum_v1` and `crypto_breakdown_v1`.
_SHALLOW_BACKUP_THRESHOLD = 50

# Sources we consider "broker primary" — these are NEVER skipped for
# being shallow. If the broker shows 9 bars on a freshly-listed
# symbol, that's what we trade against; backups don't get a vote.
_BROKER_SOURCES = frozenset({
    "webull", "webull_equity", "kraken_pro", "kraken",
})


async def pick_source(symbol: str, tf: str) -> Optional[str]:
    """Best available bar source for (symbol, tf), or None if no bars
    are on file at all.

    Selection rule (operator-pinned 2026-02-20):
        1. Walk `SOURCE_PRIORITY` in order — brokers first, then alt
           backups (polygon, finnhub_equity, thinkorswim), then
           manual ingest.
        2. Brokers are taken whenever they have ANY bars on file —
           the broker is the source of truth even if shallow.
        3. Non-broker backups are skipped if they have fewer than
           `_SHALLOW_BACKUP_THRESHOLD` bars (avoids the polygon-trial
           "9 bars on AAPL" trap that starved the Strategy Lab of
           warmup history). The next backup in priority order is
           tried.
        4. Last resort: any source on file, even shallow, so research
           never silently returns "no_bars" when SOMETHING exists.
    """
    pipeline = [
        {"$match": {"symbol": symbol, "tf": tf}},
        {"$group": {"_id": "$source", "n": {"$sum": 1}}},
    ]
    rows = await db[SHARED_OHLCV_BARS].aggregate(pipeline).to_list(None)
    if not rows:
        return None
    by_source = {r["_id"]: int(r["n"]) for r in rows}

    # Priority walk — return the first match that clears the depth
    # floor (or is a broker, which bypasses the floor).
    for src in SOURCE_PRIORITY:
        n = by_source.get(src, 0)
        if n == 0:
            continue
        if src in _BROKER_SOURCES or n >= _SHALLOW_BACKUP_THRESHOLD:
            return src

    # Nothing cleared the depth floor — fall back to whatever has the
    # most bars, even if it's a shallow backup. Still better than no
    # evidence at all.
    deepest = max(by_source.items(), key=lambda kv: kv[1])
    return deepest[0]


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
