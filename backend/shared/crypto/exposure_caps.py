"""Crypto-lane exposure caps.

Doctrine (2026-05-18, rev 3):
    Crypto trades live on Kraken. Per-order cap lifted to $1M to match
    the equity lane's lifted cap — the only authority on whether a
    crypto order routes is the **seat policy** (whoever holds the
    crypto Executor seat). The cap stays in the schema so the structure
    is here to tighten later, but it is no longer the rail that blocks
    live crypto trading.

    Day-1 caps:
      * per-order:     $1,000,000  (matches the global equity cap)
      * per-day:       inherits the global $1M cap
      * open notional: inherits the global $1M cap

    Bump the per-order ceiling here ONLY when ready to size up. The
    rest of the system reads this constant — no other code changes
    needed.
"""
from __future__ import annotations


CRYPTO_PER_ORDER_USD: float = 1_000_000.0
