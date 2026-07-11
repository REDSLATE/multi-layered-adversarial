"""Immutable market snapshot.

Every brain in a given pulse sees the SAME frozen object for a
given symbol. Contamination between brains was the silent-bug
factory the design freeze §2 called out.

Building the snapshot lives in `SnapshotService`. The dataclass
itself is trusting — validation is at the service boundary.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, Optional


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    """One symbol's market truth at one instant. Frozen and slots
    so no brain can mutate it, no brain can attach hidden fields.

    Fields:
        symbol       — uppercase ticker ("NVDA", "ETH/USD")
        lane         — "equity" | "crypto"
        timestamp    — aware UTC datetime; matches the snapshot's
                       source-bar close, not clock.now()
        price        — Decimal for exact string equality in tests;
                       brains that want float should convert
                       themselves
        indicators   — read-only Mapping of feature name → value
                       (atr, rvol, ema20, macd_hist, etc.). Comes
                       out of the feeders/indicator layer.
        market_state — coarse regime tag ("trending" | "ranging"
                       | "vol_expansion" | "quiet"). Placeholder
                       until Phase 2 SessionContext lands.
        snapshot_id  — uuid hex, carries into every OpinionEnvelope
                       built off this snapshot. Enables replay and
                       cross-brain provenance ("all four opinions
                       for snapshot_id=X").
    """
    symbol: str
    lane: str
    timestamp: datetime
    price: Decimal
    indicators: Mapping[str, float]
    market_state: str = "unknown"
    snapshot_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])


def freeze_indicators(d: Optional[dict]) -> Mapping[str, float]:
    """Wrap a plain dict in a read-only `MappingProxyType` so
    brains that dig into `snapshot.indicators` cannot mutate the
    underlying store.

    Using `MappingProxyType` (rather than `frozenset` of items or
    a custom immutable dict) gives us:
      * O(1) key access — brains iterate lots of indicators
      * `mapping["atr"]` and `.get()` still work — no surprising API
      * mutation attempts raise `TypeError` immediately
    """
    return MappingProxyType(dict(d or {}))


def build_snapshot(
    *,
    symbol: str,
    lane: str,
    timestamp: datetime,
    price: Decimal,
    indicators: Optional[dict] = None,
    market_state: str = "unknown",
) -> MarketSnapshot:
    """Factory that enforces the small handful of invariants
    (uppercase symbol, aware timestamp, indicators frozen) so
    every construction site produces a snapshot the brains can
    trust without re-checking."""
    sym = (symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol required")
    lane_lc = (lane or "").strip().lower()
    if lane_lc not in {"equity", "crypto"}:
        raise ValueError(f"lane must be 'equity' or 'crypto', got {lane!r}")
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)
    if not isinstance(price, Decimal):
        price = Decimal(str(price))
    return MarketSnapshot(
        symbol=sym,
        lane=lane_lc,
        timestamp=timestamp,
        price=price,
        indicators=freeze_indicators(indicators),
        market_state=market_state,
    )
