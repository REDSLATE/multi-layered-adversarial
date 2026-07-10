"""Universe cleanup — enforce exactly 20 active symbols per lane.

Operator directive 2026-02-19:
    * Truncate junk equity tickers (delisted / test rows / synthetic).
    * Enforce exactly 20 active symbols per lane in `patterns_universe`.
    * Real tickers not in the approved 20 are DEACTIVATED (active=False)
      rather than deleted, so operator history is preserved.
    * Truly-junk rows (nonsense symbols) are HARD-DELETED.

Approved equity 20 (large-cap momentum-eligible, operator-selected):
    AAPL, AMD, AMZN, AVGO, BABA, GOOG, META, MSFT, NFLX, NVDA,
    ORCL, PLTR, SHOP, TSLA, TSM, SPCX, GME, HOTH, TEVA, PFE

Approved crypto 20 (Kraken-tradeable majors):
    ADA/USD, AVAX/USD, BNB/USD, BTC/USD, ETH/USD, LINK/USD,
    SOL/USD, XRP/USD, DOGE/USD, DOT/USD, LTC/USD, ATOM/USD,
    ALGO/USD, XLM/USD, FIL/USD, NEAR/USD, MATIC/USD, UNI/USD,
    AAVE/USD, MKR/USD

Junk hard-delete list (nonsense symbols):
    FB       — delisted (renamed META)
    MSFY     — typo of MSFT
    HEL31138C, HEL5E7DFF   — HELLO synthetic test rows
    NDBC0764B, NDBC349F6   — NANO-BANANA synthetic test rows

Idempotent — safe to re-run. Prints a summary of what changed.
Usage:
    python -m scripts.universe_cleanup
    python /app/backend/scripts/universe_cleanup.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/backend")

from db import db


UNIVERSE = "patterns_universe"

APPROVED_EQUITY_20 = [
    "AAPL", "AMD", "AMZN", "AVGO", "BABA", "GOOG", "META", "MSFT",
    "NFLX", "NVDA", "ORCL", "PLTR", "SHOP", "TSLA", "TSM", "SPCX",
    "GME", "HOTH", "TEVA", "PFE",
]

APPROVED_CRYPTO_20 = [
    "ADA/USD", "AVAX/USD", "BNB/USD", "BTC/USD", "ETH/USD",
    "LINK/USD", "SOL/USD", "XRP/USD", "DOGE/USD", "DOT/USD",
    "LTC/USD", "ATOM/USD", "ALGO/USD", "XLM/USD", "FIL/USD",
    "NEAR/USD", "MATIC/USD", "UNI/USD", "AAVE/USD", "MKR/USD",
]

# Nonsense/synthetic rows — HARD DELETE. Delisted real tickers
# (like FB) also go here because operator explicitly listed them.
HARD_DELETE = [
    "FB", "MSFY",
    "HEL31138C", "HEL5E7DFF",
    "NDBC0764B", "NDBC349F6",
]

_NOTE = "universe-cleanup 2026-02-19 (canonical 20/lane)"
_ADDED_BY = "system:universe_cleanup"


async def _now_iso():
    return datetime.now(timezone.utc).isoformat()


async def run():
    now = await _now_iso()
    summary = {
        "hard_deleted": [],
        "upserted_equity_active": [],
        "upserted_crypto_active": [],
        "deactivated_equity": [],
        "deactivated_crypto": [],
    }

    # 1. Hard-delete junk rows.
    del_result = await db[UNIVERSE].delete_many({"symbol": {"$in": HARD_DELETE}})
    summary["hard_deleted"] = HARD_DELETE
    print(f"[1/4] hard-deleted junk: {del_result.deleted_count} rows "
          f"matching {HARD_DELETE}")

    # 2. Upsert the approved 20 equities as active=True.
    for sym in APPROVED_EQUITY_20:
        await db[UNIVERSE].update_one(
            {"symbol": sym},
            {
                "$set": {
                    "symbol": sym,
                    "lane": "equity",
                    "active": True,
                    "note": _NOTE,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "added_by": _ADDED_BY,
                    "added_at": now,
                },
            },
            upsert=True,
        )
        summary["upserted_equity_active"].append(sym)
    print(f"[2/4] upserted {len(APPROVED_EQUITY_20)} approved "
          f"equity symbols as active=True")

    # 3. Upsert the approved 20 crypto majors as active=True.
    for sym in APPROVED_CRYPTO_20:
        await db[UNIVERSE].update_one(
            {"symbol": sym},
            {
                "$set": {
                    "symbol": sym,
                    "lane": "crypto",
                    "active": True,
                    "note": _NOTE,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "added_by": _ADDED_BY,
                    "added_at": now,
                },
            },
            upsert=True,
        )
        summary["upserted_crypto_active"].append(sym)
    print(f"[3/4] upserted {len(APPROVED_CRYPTO_20)} approved "
          f"crypto symbols as active=True")

    # 4. Deactivate real tickers that survived hard-delete but are
    #    NOT on the approved list. Preserve their history (don't
    #    delete), just flip active=False.
    r_eq = await db[UNIVERSE].update_many(
        {
            "lane": "equity",
            "symbol": {"$nin": APPROVED_EQUITY_20},
            "active": True,
        },
        {"$set": {
            "active": False,
            "note": _NOTE,
            "updated_at": now,
            "deactivated_by": _ADDED_BY,
        }},
    )
    r_cr = await db[UNIVERSE].update_many(
        {
            "lane": "crypto",
            "symbol": {"$nin": APPROVED_CRYPTO_20},
            "active": True,
        },
        {"$set": {
            "active": False,
            "note": _NOTE,
            "updated_at": now,
            "deactivated_by": _ADDED_BY,
        }},
    )
    print(f"[4/4] deactivated equity={r_eq.modified_count} "
          f"crypto={r_cr.modified_count} out-of-list rows")

    # Post-cleanup verification.
    active_eq = await db[UNIVERSE].count_documents(
        {"lane": "equity", "active": True},
    )
    active_cr = await db[UNIVERSE].count_documents(
        {"lane": "crypto", "active": True},
    )
    total = await db[UNIVERSE].count_documents({})
    print(f"\n=== FINAL STATE ===")
    print(f"active equity: {active_eq} (target 20)")
    print(f"active crypto: {active_cr} (target 20)")
    print(f"total rows (all states): {total}")
    if active_eq != 20 or active_cr != 20:
        print("\n!!! WARNING: active count does not match target 20 !!!")
        print("Inspect `patterns_universe` for lane-missing rows or "
              "duplicates that survived the upsert.")


if __name__ == "__main__":
    asyncio.run(run())
