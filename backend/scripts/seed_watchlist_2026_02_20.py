"""One-shot seed for `paradox_watchlist` — top 50 Webull equities + top 30 Kraken USD crypto pairs.

Operator directive (2026-02-20): paradox_watchlist was empty (previous
agent compiled but never inserted). This script seeds the watchlist
the scanner's PRIMARY universe source reads from, so brain logic
finally has something to monitor and emit intents against.

Run idempotently: existing rows are SKIPPED (upsert on `symbol`).

    cd /app/backend && set -a && source .env && set +a && \
        python scripts/seed_watchlist_2026_02_20.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/backend")

from db import db  # noqa: E402
from namespaces import PARADOX_WATCHLIST  # noqa: E402


# Top 50 most-liquid US large-caps tradable on Webull. The equity lane
# resolver uses a rule-based fallback so any alphanumeric ticker here
# auto-maps to Webull's bare-ticker form — no broker_symbol_resolver
# code change required to add or remove from this list.
EQUITIES_50 = [
    # Mega-caps
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO",
    "LLY", "JPM",
    # Large-caps — financials, consumer, healthcare
    "V", "UNH", "XOM", "WMT", "MA", "PG", "JNJ", "ORCL", "HD", "COST",
    "ABBV", "BAC", "CVX", "NFLX", "MRK",
    # Consumer + tech rotation
    "KO", "CRM", "AMD", "PEP", "ADBE", "TMO", "CSCO", "ACN", "MCD",
    "ABT", "LIN", "WFC", "DIS", "INTC", "QCOM",
    # Industrials + high-beta names operator follows
    "IBM", "CAT", "TXN", "GE", "BA", "PYPL", "UBER", "PLTR",
    # Crypto-exposure proxies (user explicitly tracks MSTR)
    "COIN", "MSTR",
]

# Top 30 USD pairs on Kraken (volume-weighted, Feb 2026). Each entry
# MUST be present in shared/broker_symbol_resolver.BROKER_SYMBOL_MAP
# ["kraken"] — otherwise broker_router NO_TRADEs the intent.
CRYPTO_30 = [
    "BTC-USD",   "ETH-USD",   "SOL-USD",   "XRP-USD",   "DOGE-USD",
    "ADA-USD",   "AVAX-USD",  "DOT-USD",   "LINK-USD",  "LTC-USD",
    "BCH-USD",   "MATIC-USD", "ATOM-USD",  "NEAR-USD",  "APT-USD",
    "ARB-USD",   "OP-USD",    "UNI-USD",   "AAVE-USD",  "INJ-USD",
    "FIL-USD",   "ALGO-USD",  "XLM-USD",   "TRX-USD",   "TIA-USD",
    "SUI-USD",   "SEI-USD",   "WIF-USD",   "PEPE-USD",  "SHIB-USD",
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def seed() -> None:
    coll = db[PARADOX_WATCHLIST]
    rows: list[dict] = []
    for sym in EQUITIES_50:
        rows.append({
            "symbol": sym,
            "lane": "equity",
            "active": True,
            "note": "seed_2026_02_20_top50_webull",
            "added_at": _now(),
        })
    for sym in CRYPTO_30:
        rows.append({
            "symbol": sym,
            "lane": "crypto",
            "active": True,
            "note": "seed_2026_02_20_top30_kraken",
            "added_at": _now(),
        })

    inserted = 0
    skipped = 0
    for row in rows:
        existing = await coll.find_one({"symbol": row["symbol"]})
        if existing:
            skipped += 1
            continue
        await coll.insert_one(row)
        inserted += 1

    total = await coll.count_documents({})
    active = await coll.count_documents({"active": True})
    eq = await coll.count_documents({"lane": "equity"})
    cr = await coll.count_documents({"lane": "crypto"})
    print(
        f"watchlist seed complete: inserted={inserted} skipped={skipped} "
        f"total={total} active={active} equity={eq} crypto={cr}"
    )


if __name__ == "__main__":
    asyncio.run(seed())
