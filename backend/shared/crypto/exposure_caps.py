"""Crypto-lane exposure caps.

Doctrine (2026-02-16):
    Crypto goes LIVE on Kraken with a hard $30 per-order ceiling
    (raised from $10 on 2026-02-15). This file owns that number. The
    shared `exposure_caps.py` dispatcher pulls it via
    `CRYPTO_PER_ORDER_USD` so the equity tree never imports anything
    crypto-specific.

    Day-1 caps:
      * per-order:  $30   (tight — Kraken is real money)
      * per-day:    inherits the global $1M cap (effectively unbounded for now)
      * open notional: inherits the global $1M cap

    Bump the per-order ceiling here ONLY when ready to size up. The
    rest of the system reads this constant — no other code changes
    needed.
"""
from __future__ import annotations


CRYPTO_PER_ORDER_USD: float = 30.0
