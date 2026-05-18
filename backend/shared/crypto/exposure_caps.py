"""Crypto-lane exposure caps.

Doctrine (2026-05-18, rev 4):
    Per-order cap is $500 — generous enough for normal sizing, tight
    enough that a brain bug emitting a huge notional can't blow up
    the account. Live order routing is gated by seat policy; the cap
    is operational insurance, not a doctrinal restriction.

    Bump this only by deliberate operator order.
"""
from __future__ import annotations


CRYPTO_PER_ORDER_USD: float = 500.0
