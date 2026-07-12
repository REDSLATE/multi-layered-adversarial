"""Pulse-orchestrator manifest persistence wiring tests.

Pins the boundary from the 2026-07 packet:

    * Brain records diagnostic material inside `evaluate` (via
      the `_last_hint` state that `take_manifest_hint` drains).
    * Orchestrator writes the manifest — the brain never touches
      Mongo.
    * ParityKey on the envelope and on the manifest match
      exactly (both derived from `snapshot.bar_identity`).
    * INSUFFICIENT_DATA opinions ALSO get manifests, so the
      parity endpoint can see starved-input events.
    * A snapshot without a `bar_identity` produces a written
      envelope with `parity_key_str=""` and NO manifest write
      (the parity endpoint later reports `parity_key_missing`).

These tests inject a fake `persist_manifest` to avoid touching
Mongo — verifying the orchestrator CALL SHAPE is enough; the
persist function itself is already fail-soft against Mongo
failures.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest

from mc_arbiter.models import Direction, OpinionStatus
from mc_brains.camino import CaminoBrain
from mc_pulse import pulse as pulse_mod
from mc_pulse.parity_key import intraday_bar_identity
from mc_pulse.protocols import Brain
from mc_pulse.registry import BrainRegistry, set_registry
from mc_pulse.snapshot import MarketSnapshot, build_snapshot


def _bar():
    ts = datetime(2026, 7, 11, 15, 0, 0, tzinfo=timezone.utc)
    return intraday_bar_identity(
        timeframe="1m", bar_timestamp=ts,
        timestamp_semantics="open", source="shared_ohlcv_bars",
    )


def _snapshot(*, features: dict, symbol: str = "NVDA",
              with_bar_identity: bool = True) -> MarketSnapshot:
    bar = _bar() if with_bar_identity else None
    return build_snapshot(
        symbol=symbol,
        lane="equity",
        timestamp=datetime(2026, 7, 11, 15, 0, 0, tzinfo=timezone.utc),
        price=Decimal("195.0"),
        indicators={},
        market_state="trending",
        feature_snapshot=features,
        source_tf="1m",
        source_bar_count=30,
        bar_identity=bar,
        source_bar_id="bar-1",
    )


def _full_features():
    return {
        "trend_score": 0.42,
        "price_change_pct": 1.23,
        "volume_change_pct": 8.1,
        "rsi": 55.0,
        "spread_bps": 3.0,
        "volatility": 0.15,
        "liquidity_score": 0.85,
        "setup_score": 0.65,
        "market_regime": "trending",
        "spread_quality": "live",
        "price": 195.0,
    }


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Every test in this file installs its own registry so the
    global one doesn't leak state between tests."""
    saved = set_registry(BrainRegistry())
    try:
        yield
    finally:
        set_registry(saved)


# ─────────────────────── manifest persistence path ─────────────────────


@pytest.mark.asyncio
async def test_pulse_persists_manifest_for_ok_opinion(monkeypatch):
    """A normal Camino evaluation with a full feature snapshot
    must produce ONE manifest write with status=OK and matching
    ParityKey."""
    brain = CaminoBrain()
    reg = BrainRegistry()
    reg.register(brain)
    set_registry(reg)

    snap = _snapshot(features=_full_features())

    persist_calls = []

    async def fake_persist(manifest):
        persist_calls.append(manifest)

    async def fake_upsert_envelopes(*a, **k):
        return None

    async def fake_persist_receipt(*a, **k):
        return None

    with patch.object(pulse_mod, "persist_manifest", fake_persist), \
         patch.object(pulse_mod, "_upsert_envelopes", fake_upsert_envelopes), \
         patch("mc_pulse.pulse.persist_receipt", fake_persist_receipt):
        await pulse_mod.pulse_tick([snap], cadence_seconds=15)

    assert len(persist_calls) == 1
    m = persist_calls[0]
    assert m.path == "pulse"
    assert m.status == OpinionStatus.OK.value
    assert m.parity_key.symbol == "NVDA"
    assert m.parity_key.timeframe == "1m"
    assert m.action in {"BUY", "SELL", "HOLD"}


@pytest.mark.asyncio
async def test_pulse_persists_manifest_for_insufficient_data(monkeypatch):
    """A starved-input evaluation ALSO gets a manifest — that's
    the whole point of the fix. Parity math would otherwise
    silently drop the exact events we're trying to diagnose."""
    brain = CaminoBrain()
    reg = BrainRegistry()
    reg.register(brain)
    set_registry(reg)

    # 2026-07-12 (P7a): the manifest-INSUFFICIENT_DATA path now
    # fires when the STRATEGY's primary feature is absent. For
    # Camino (TrendFollowingStrategy) that's `trend_score`.
    snap = _snapshot(features={"price_change_pct": 0.5})  # trend_score absent

    persist_calls = []

    async def fake_persist(manifest):
        persist_calls.append(manifest)

    async def fake_upsert_envelopes(*a, **k):
        return None

    async def fake_persist_receipt(*a, **k):
        return None

    with patch.object(pulse_mod, "persist_manifest", fake_persist), \
         patch.object(pulse_mod, "_upsert_envelopes", fake_upsert_envelopes), \
         patch("mc_pulse.pulse.persist_receipt", fake_persist_receipt):
        await pulse_mod.pulse_tick([snap], cadence_seconds=15)

    assert len(persist_calls) == 1
    m = persist_calls[0]
    assert m.status == OpinionStatus.INSUFFICIENT_DATA.value
    assert m.confidence == 0.0
    assert m.action == "HOLD"
    assert "TREND_NO_SIGNAL" in m.reason_codes


@pytest.mark.asyncio
async def test_envelope_carries_parity_key_str():
    """The pulse-side envelope must ship the ParityKey so
    `mc_opinions_compare` rows can be paired with manifests and
    with runner-side rows on the same canonical bar."""
    brain = CaminoBrain()
    reg = BrainRegistry()
    reg.register(brain)
    set_registry(reg)

    snap = _snapshot(features=_full_features())

    captured = []

    async def fake_upsert_envelopes(envelopes, _collection):
        captured.extend(envelopes)

    async def fake_persist(_m):
        return None

    async def fake_persist_receipt(*a, **k):
        return None

    with patch.object(pulse_mod, "_upsert_envelopes", fake_upsert_envelopes), \
         patch.object(pulse_mod, "persist_manifest", fake_persist), \
         patch("mc_pulse.pulse.persist_receipt", fake_persist_receipt):
        await pulse_mod.pulse_tick([snap], cadence_seconds=15)

    assert len(captured) == 1
    env = captured[0]
    assert env.parity_key_str, "envelope missing parity_key_str"
    # Same ParityKey the manifest would compute — pulse and
    # manifest must join on the exact same string.
    expected = snap.bar_identity.to_parity_key(
        brain_id="camino", symbol="NVDA",
    ).as_string()
    assert env.parity_key_str == expected


# ─────────────────────── missing BarIdentity path ─────────────────────


@pytest.mark.asyncio
async def test_snapshot_without_bar_identity_skips_manifest(monkeypatch):
    """When a snapshot has no `bar_identity` (test fixture / cold
    boot / legacy caller), NO manifest is persisted and the
    envelope's parity_key_str stays empty. The parity endpoint
    will surface such rows as `parity_key_missing`."""
    brain = CaminoBrain()
    reg = BrainRegistry()
    reg.register(brain)
    set_registry(reg)

    snap = _snapshot(features=_full_features(), with_bar_identity=False)

    persist_calls = []
    captured_envelopes = []

    async def fake_persist(m):
        persist_calls.append(m)

    async def fake_upsert(envs, _c):
        captured_envelopes.extend(envs)

    async def fake_persist_receipt(*a, **k):
        return None

    with patch.object(pulse_mod, "persist_manifest", fake_persist), \
         patch.object(pulse_mod, "_upsert_envelopes", fake_upsert), \
         patch("mc_pulse.pulse.persist_receipt", fake_persist_receipt):
        await pulse_mod.pulse_tick([snap], cadence_seconds=15)

    assert len(persist_calls) == 0, "no manifest when bar_identity is None"
    assert len(captured_envelopes) == 1
    assert captured_envelopes[0].parity_key_str == ""


# ─────────────────────── non-Camino brain safety ─────────────────────


class _DummyBrain:
    """A brain with no `take_manifest_hint` — the orchestrator
    must NOT touch it. Represents pre-migration brains during
    the pilot window."""
    id = "dummy"
    lanes = frozenset({"equity"})
    cadence_seconds = 30
    evaluation_timeout_seconds = 2.0

    def should_evaluate(self, *, now, snapshot):
        return True

    async def evaluate(self, snapshot):
        return None    # nothing to say — no envelope, no hint


@pytest.mark.asyncio
async def test_pulse_ignores_brains_without_manifest_hints():
    """Non-Camino brains during the migration must not crash the
    hint drain step. Only brains that expose `take_manifest_hint`
    are drained; others pass through cleanly."""
    dummy = _DummyBrain()
    reg = BrainRegistry()
    reg.register(dummy)
    set_registry(reg)

    snap = _snapshot(features=_full_features())

    persist_calls = []

    async def fake_persist(m):
        persist_calls.append(m)

    async def fake_upsert(*a, **k):
        return None

    async def fake_persist_receipt(*a, **k):
        return None

    with patch.object(pulse_mod, "persist_manifest", fake_persist), \
         patch.object(pulse_mod, "_upsert_envelopes", fake_upsert), \
         patch("mc_pulse.pulse.persist_receipt", fake_persist_receipt):
        receipt = await pulse_mod.pulse_tick([snap], cadence_seconds=15)

    assert len(persist_calls) == 0
    # The dummy brain "completed" cleanly (returned None) — must
    # still be counted as completed, not failed.
    assert "dummy" in receipt.brains_completed
