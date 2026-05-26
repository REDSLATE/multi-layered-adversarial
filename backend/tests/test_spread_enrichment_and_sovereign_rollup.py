"""Tripwires — spread_bps enrichment + sovereign rollup (2026-05-26).

Doctrine:
    1. Brain-supplied spread_bps wins.
    2. Bid/ask present → derived correctly.
    3. Indicator cache fallback works within freshness window.
    4. Kraken fallback opt-in only (default OFF).
    5. Final fallback is the sentinel + `spread_source=sentinel_unknown`.

    Sovereign rollup doctrine:
    6. Sovereign-history rows are recognized (mode + learning_rate + brain).
    7. Sovereign movement = "snapshot".
    8. Sovereign event reflects clamp/apply/no-change with sign.
    9. Slim rollup preserves mode, applied delta, and clamp flag.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from db import db
from shared.calibration.snapshot_contract import SPREAD_BPS_UNKNOWN
from shared.market_data import (
    SRC_BRAIN,
    SRC_MC_DERIVED,
    SRC_MC_INDICATOR_CACHE,
    SRC_SENTINEL,
    enrich_snapshot_spread,
)
from shared.storage_rollup.derive import derive_event, derive_movement
from shared.storage_rollup.runner import _build_rollup_doc


pytestmark = pytest.mark.asyncio


# ─────────── A — spread enrichment ladder ───────────


async def test_brain_supplied_spread_wins():
    snap = {"spread_bps": 12.5, "bid": 99, "ask": 100}
    enriched, diag = await enrich_snapshot_spread(snap, symbol="AAPL", lane="equity")
    assert enriched["spread_bps"] == 12.5
    assert enriched["spread_source"] == SRC_BRAIN


async def test_derive_from_bid_ask_when_brain_silent():
    snap = {"bid": 99.95, "ask": 100.05}
    enriched, diag = await enrich_snapshot_spread(
        snap, symbol="AAPL", lane="equity",
    )
    assert enriched["spread_source"] == SRC_MC_DERIVED
    assert enriched["spread_bps"] > 0
    assert enriched["spread_bps"] < 100  # sane bound


async def test_brain_sentinel_falls_through_to_derived():
    """If the brain explicitly ships SPREAD_BPS_UNKNOWN (its own 'I
    don't know') and bid/ask are available, MC SHOULD derive rather
    than honor the give-up sentinel."""
    snap = {"spread_bps": SPREAD_BPS_UNKNOWN, "bid": 99.5, "ask": 100.5}
    enriched, _ = await enrich_snapshot_spread(
        snap, symbol="ETH/USD", lane="crypto",
    )
    assert enriched["spread_source"] == SRC_MC_DERIVED


async def test_sentinel_when_no_source_has_data():
    snap: dict = {}    # nothing at all
    enriched, diag = await enrich_snapshot_spread(
        snap, symbol="NONEXISTENT_SYMBOL_XYZQ", lane="crypto",
    )
    assert enriched["spread_bps"] == SPREAD_BPS_UNKNOWN
    assert enriched["spread_source"] == SRC_SENTINEL


async def test_diagnostics_carry_attempt_trail():
    snap: dict = {}
    _, diag = await enrich_snapshot_spread(
        snap, symbol="WHATEVER", lane="equity",
    )
    sources = [a["source"] for a in diag["attempts"]]
    assert SRC_BRAIN in sources
    assert SRC_MC_DERIVED in sources
    assert SRC_SENTINEL in sources
    assert diag["elapsed_ms"] >= 0


async def test_indicator_cache_fallback():
    """If MC's `shared_indicator_snapshots` has a fresh row with bid/ask,
    use it."""
    from namespaces import SHARED_INDICATOR_SNAPSHOTS
    sym = "SPRD_TEST_SYM"
    await db[SHARED_INDICATOR_SNAPSHOTS].delete_many({"symbol": sym})
    await db[SHARED_INDICATOR_SNAPSHOTS].insert_one({
        "symbol": sym, "source": "test", "tf": "1m",
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "indicators": {"bid": 99.50, "ask": 100.50},
    })
    enriched, _ = await enrich_snapshot_spread(
        {}, symbol=sym, lane="equity",
    )
    assert enriched["spread_source"] == SRC_MC_INDICATOR_CACHE
    assert enriched["spread_bps"] > 0
    await db[SHARED_INDICATOR_SNAPSHOTS].delete_many({"symbol": sym})


async def test_indicator_cache_stale_ignored():
    """Old indicator rows (beyond freshness window) MUST NOT contribute."""
    from namespaces import SHARED_INDICATOR_SNAPSHOTS
    sym = "SPRD_STALE_SYM"
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    await db[SHARED_INDICATOR_SNAPSHOTS].delete_many({"symbol": sym})
    await db[SHARED_INDICATOR_SNAPSHOTS].insert_one({
        "symbol": sym, "source": "test", "tf": "1m",
        "computed_at": old.isoformat(),
        "indicators": {"bid": 99.50, "ask": 100.50},
    })
    enriched, _ = await enrich_snapshot_spread(
        {}, symbol=sym, lane="equity",
    )
    assert enriched["spread_source"] == SRC_SENTINEL
    await db[SHARED_INDICATOR_SNAPSHOTS].delete_many({"symbol": sym})


def test_compute_spread_bps_canonical():
    """Re-export sanity: the canonical helper still does what we
    documented in the contract."""
    from shared.calibration.snapshot_contract import compute_spread_bps
    # 100 bid, 100.10 ask → mid 100.05 → diff 0.10 → bps = (0.10/100.05) * 10000 ≈ 10
    v = compute_spread_bps(100.0, 100.10)
    assert 9 < v < 11


# ─────────── B — sovereign rollup derivation ───────────


def _sov(brain="alpha", clamped=False, raw=0.0, applied=0.0, mode="DTD"):
    return {
        "brain": brain, "mode": mode, "learning_rate": 0.05,
        "delta_was_clamped": clamped,
        "raw_confidence_delta": raw,
        "confidence_delta": applied,
    }


def test_sovereign_recognized_as_snapshot_movement():
    assert derive_movement(_sov()) == "snapshot"


def test_sovereign_clamped_positive_event():
    assert derive_event(_sov(clamped=True, raw=0.3)) == "delta_clamped_pos"


def test_sovereign_clamped_negative_event():
    assert derive_event(_sov(clamped=True, raw=-0.5)) == "delta_clamped_neg"


def test_sovereign_clamped_zero_event():
    assert derive_event(_sov(clamped=True, raw=0.0)) == "delta_clamped_zero"


def test_sovereign_applied_positive_event():
    assert derive_event(_sov(applied=0.05)) == "delta_applied_pos"


def test_sovereign_applied_negative_event():
    assert derive_event(_sov(applied=-0.05)) == "delta_applied_neg"


def test_sovereign_no_change_event():
    assert derive_event(_sov(applied=0.0)) == "no_change"


def test_non_sovereign_row_doesnt_get_snapshot_movement():
    """A regular intent row MUST NOT be misclassified as sovereign
    just because the heuristic checks `mode`+`learning_rate`+`brain`.
    Real intents lack `learning_rate`."""
    assert derive_movement({"action": "BUY", "brain": "alpha"}) == "long"


def test_sovereign_rollup_doc_preserves_analytical_fields():
    """Slim sovereign rollup MUST keep mode, deltas, clamp flag — the
    operator's full analytical surface."""
    row = _sov(brain="camaro", clamped=True, raw=0.4, applied=0.10, mode="DTD")
    row["learning_rate"] = 0.08
    row["posted_as"] = "executor"
    row["seat_epoch"] = 42
    doc = _build_rollup_doc(
        row, "sovereign_state_history", "snapshot", "delta_clamped_pos",
    )
    assert doc["mode"] == "DTD"
    assert doc["confidence_delta"] == 0.10
    assert doc["raw_confidence_delta"] == 0.4
    assert doc["delta_was_clamped"] is True
    assert doc["learning_rate"] == 0.08
    assert doc["posted_as"] == "executor"
    assert doc["seat_epoch"] == 42
    # Verbose payload still NOT present.
    assert "weights" not in doc
    assert "recent_outcomes" not in doc
    assert "notes" not in doc


def test_intent_rollup_doc_does_not_carry_sovereign_fields():
    """Non-sovereign rollup MUST NOT carry `mode`/`learning_rate`/etc.
    These are reserved for sovereign-history rows only."""
    intent_row = {
        "action": "BUY", "stack": "alpha", "symbol": "AAPL",
        "gate_state": "passed", "executed": False,
    }
    doc = _build_rollup_doc(
        intent_row, "shared_intents", "long", "shadow_observation",
    )
    assert "mode" not in doc
    assert "learning_rate" not in doc
    assert "delta_was_clamped" not in doc


# ─────────── C — TTL drop script ───────────


async def test_drop_ttl_idempotent():
    from scripts.drop_sovereign_history_ttl import drop_ttl
    # Dry-run twice — must not error.
    r1 = await drop_ttl(dry_run=True)
    r2 = await drop_ttl(dry_run=True)
    assert "index" in r1 and "index" in r2
