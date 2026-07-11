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

from mc_pulse.parity_key import BarIdentity
from mc_pulse.freshness import SnapshotHealth


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
        position_context — per-brain current holdings for THIS
                       symbol, as `Mapping[brain_id, dict]`. Read-
                       only. Brains look up ONLY their own row
                       (self.id) — they don't peek at peers'
                       positions. Runner audit row #7. Empty
                       mapping is the honest "we hold nothing here."
        source_tf    — bar timeframe the snapshot was built from
                       ("1m", "5m", "1d"). Feeds ParityKey bucket
                       alignment. Empty means "unknown / synthetic".
        source_bar_count — how many raw bars the indicator layer
                       had available. Manifest field — a runner
                       building on 120 bars vs a pulse building
                       on 3 bars are NOT comparable inputs.
    """
    symbol: str
    lane: str
    timestamp: datetime
    price: Decimal
    indicators: Mapping[str, float]
    market_state: str = "unknown"
    snapshot_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    position_context: Mapping[str, dict] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    source_tf: str = ""
    source_bar_count: int = 0
    # Authoritative bar identity from the source record — feeds
    # ParityKey composition on both runner and pulse paths. Never
    # `None` at construction time; sentinel default lets tests
    # build minimal snapshots without threading a full BarIdentity.
    bar_identity: Optional[BarIdentity] = None
    source_bar_id: str = ""
    # Full feature dict as produced by the canonical Camino
    # feature builder. `indicators` (above) keeps the narrow
    # "known-good numeric features" view for non-Camino brains
    # still under migration. `feature_snapshot` carries every
    # field the legacy brain core reads so the pulse Camino
    # adapter can call the core without an impoverished input.
    feature_snapshot: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({}),
    )
    # True when the canonical builder took its cold-start branch
    # (bars < 20). Manifest-facing — a runner-hot vs pulse-cold
    # divergence is itself a parity finding and must not be
    # averaged into aggregate metrics.
    fallback_used: bool = False
    # Freshness verdict. Attached at snapshot construction; the
    # pulse orchestrator gates on `health.is_fresh` BEFORE calling
    # `brain.evaluate` — stale snapshots produce NO opinion. See
    # `mc_pulse.freshness` for the contract.
    health: Optional[SnapshotHealth] = None


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
    position_context: Optional[dict] = None,
    source_tf: str = "",
    source_bar_count: int = 0,
    bar_identity: Optional[BarIdentity] = None,
    source_bar_id: str = "",
    feature_snapshot: Optional[dict] = None,
    fallback_used: bool = False,
    health: Optional[SnapshotHealth] = None,
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
        position_context=MappingProxyType(dict(position_context or {})),
        source_tf=source_tf,
        source_bar_count=int(source_bar_count or 0),
        bar_identity=bar_identity,
        source_bar_id=source_bar_id or "",
        feature_snapshot=MappingProxyType(dict(feature_snapshot or {})),
        fallback_used=bool(fallback_used),
        health=health,
    )
