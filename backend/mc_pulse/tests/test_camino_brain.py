"""CaminoBrain pulse-adapter contract tests.

Pins the two behavior changes from the 2026-07 parity packet:

    1. Missing required field → INSUFFICIENT_DATA opinion with
       confidence=0.0, NOT a HOLD@1.0 that masquerades as
       confident agreement. This is the primary correction for
       the "100% HOLD @ 1.00" signature the operator flagged.

    2. `take_manifest_hint(symbol)` drains diagnostic material
       populated inside `evaluate` — the brain never persists;
       the pulse orchestrator does. Pop semantics: each hint is
       consumed exactly once.

Also asserts the doctrine: the brain does not touch Mongo, HTTP,
or any data source. All fetches belong to MC.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import MappingProxyType

import pytest

from mc_arbiter.models import Direction, OpinionStatus
from mc_brains.camino import CaminoBrain, CaminoManifestHint
from mc_pulse.snapshot import MarketSnapshot, build_snapshot


def _full_features():
    return {
        "trend_score": 0.42,
        "price_change_pct": 1.23,
        "volume_change_pct": 8.1,
        "rsi": 55.0,
        "spread_bps": 3.0,
        "volatility": 0.15,
        "liquidity_score": 0.85,
        "setup_score": 0.71,
        "market_regime": "trending",
        "spread_quality": "live",
        "price": 195.0,
    }


def _snap(*, features: dict, symbol: str = "NVDA", lane: str = "equity",
          fallback_used: bool = False) -> MarketSnapshot:
    return build_snapshot(
        symbol=symbol,
        lane=lane,
        timestamp=datetime(2026, 7, 11, 15, 0, 0, tzinfo=timezone.utc),
        price=Decimal("195.0"),
        indicators={},
        market_state="trending",
        feature_snapshot=features,
        source_tf="1m",
        source_bar_count=30,
        fallback_used=fallback_used,
    )


# ─────────────────────── INSUFFICIENT_DATA path ─────────────────────


@pytest.mark.asyncio
async def test_missing_required_field_emits_insufficient_data():
    """The primary fix — no more HOLD @ 1.0 masquerade."""
    brain = CaminoBrain()
    snap = _snap(features={"trend_score": 0.4})  # everything else missing
    opinion = await brain.evaluate(snap)
    assert opinion is not None
    assert opinion.status == OpinionStatus.INSUFFICIENT_DATA.value
    assert opinion.confidence == 0.0
    assert opinion.direction == Direction.FLAT
    assert "MISSING_REQUIRED_FEATURES" in opinion.reason_codes


@pytest.mark.asyncio
async def test_insufficient_data_names_specific_missing_fields():
    """Diagnosis requires knowing WHICH fields were absent."""
    brain = CaminoBrain()
    snap = _snap(features={"trend_score": 0.4, "spread_bps": 3.0})
    opinion = await brain.evaluate(snap)
    assert opinion is not None
    # Reason codes carry the specific missing field names (up to 6)
    missing_in_codes = [c for c in opinion.reason_codes
                        if c != "MISSING_REQUIRED_FEATURES"]
    assert "volume_change_pct" in missing_in_codes
    assert "volatility" in missing_in_codes


@pytest.mark.asyncio
async def test_insufficient_data_records_manifest_hint():
    """Even when the core is skipped, the pulse must be able to
    persist a manifest — otherwise starved-input rows never
    surface in the parity endpoint."""
    brain = CaminoBrain()
    snap = _snap(features={"trend_score": 0.4})
    await brain.evaluate(snap)
    hint = brain.take_manifest_hint("NVDA")
    assert hint is not None
    assert hint.status == OpinionStatus.INSUFFICIENT_DATA.value
    assert hint.confidence == 0.0
    assert hint.action == "HOLD"
    assert "MISSING_REQUIRED_FEATURES" in hint.reason_codes


# ─────────────────────── OK path ─────────────────────


@pytest.mark.asyncio
async def test_full_features_invokes_core_and_produces_ok_opinion():
    brain = CaminoBrain()
    snap = _snap(features=_full_features())
    opinion = await brain.evaluate(snap)
    assert opinion is not None
    assert opinion.status == OpinionStatus.OK.value
    # confidence must be a REAL number in [0, 1], not the
    # pathological 1.0 the starved-input path produced.
    assert 0.0 <= opinion.confidence <= 1.0
    hint = brain.take_manifest_hint("NVDA")
    assert hint is not None
    assert hint.status == OpinionStatus.OK.value


# ─────────────────────── hint bookkeeping ─────────────────────


@pytest.mark.asyncio
async def test_take_manifest_hint_is_pop_semantics():
    """A hint may be consumed exactly once. A stale hint from a
    prior tick MUST NOT be reused."""
    brain = CaminoBrain()
    await brain.evaluate(_snap(features={"trend_score": 0.1}))
    first = brain.take_manifest_hint("NVDA")
    second = brain.take_manifest_hint("NVDA")
    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_hints_are_scoped_per_symbol():
    """Concurrent evaluations across symbols must not collide."""
    brain = CaminoBrain()
    await brain.evaluate(_snap(features={"trend_score": 0.1}, symbol="NVDA"))
    await brain.evaluate(_snap(features={"trend_score": 0.2}, symbol="MSFT"))
    h_nvda = brain.take_manifest_hint("NVDA")
    h_msft = brain.take_manifest_hint("MSFT")
    assert h_nvda is not None
    assert h_msft is not None
    # Both hints record their respective snapshot's trend_score.
    # We can't assert numerical equality (the core mutates
    # feature_snapshot before returning), but we can assert both
    # are non-None distinct objects.
    assert h_nvda is not h_msft


# ─────────────────────── protocol invariants ─────────────────────


def test_brain_declares_pulse_protocol_fields():
    b = CaminoBrain()
    assert b.id == "camino"
    assert b.lanes == frozenset({"equity", "crypto"})
    assert b.cadence_seconds == 30
    assert b.evaluation_timeout_seconds == 2.0


def test_camino_module_does_no_io():
    """Doctrine: brains never fetch data. If someone adds a db /
    httpx import to CaminoBrain during a future refactor, this
    tripwire fires."""
    import mc_brains.camino as camino_mod
    src = open(camino_mod.__file__).read()
    forbidden = ("from db import", "import httpx", "import requests",
                 "AsyncIOMotorClient", "MongoClient")
    for pattern in forbidden:
        assert pattern not in src, f"CaminoBrain must not do I/O: found {pattern!r}"


# ─────────────────────── should_evaluate cadence ─────────────────────


def test_should_evaluate_respects_cadence():
    brain = CaminoBrain()
    snap = _snap(features=_full_features())
    t0 = datetime(2026, 7, 11, 15, 0, 0, tzinfo=timezone.utc)
    t_early = datetime(2026, 7, 11, 15, 0, 15, tzinfo=timezone.utc)  # +15s
    t_late = datetime(2026, 7, 11, 15, 0, 45, tzinfo=timezone.utc)   # +45s
    assert brain.should_evaluate(now=t0, snapshot=snap) is True
    # Within 30s cadence → no-op tick.
    assert brain.should_evaluate(now=t_early, snapshot=snap) is False
    # After cadence → allowed again.
    assert brain.should_evaluate(now=t_late, snapshot=snap) is True
