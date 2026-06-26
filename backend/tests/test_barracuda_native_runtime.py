"""Regression tests for the Barracuda native runtime (in-process brain).

Covers three layers:
  1. strategy.evaluate — pure compute, no I/O, exhaustive branch tests
  2. runner.tick_once  — DB roundtrip with a real (in-memory) Mongo,
                         emitting via the canonical `submit_intent_in_process`
  3. scheduler         — flag-gating (default OFF), env tunable tick

All tests use the live preview Mongo via the same `db` import the rest
of the backend uses. They write into the real `shared_intents` and
`barracuda_native_runtime_ticks` collections, so they pre-clean by
test-symbol prefix to stay idempotent.
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, "/app/backend")

from shared.brains.barracuda import strategy  # noqa: E402
from shared.brains.barracuda import runner  # noqa: E402
from shared.runtime import barracuda_runtime  # noqa: E402


# ────────────────────────── strategy ──────────────────────────


def _ready_indicators(**overrides):
    """A minimal complete indicators dict the strategy will accept."""
    base = {
        "ready": True,
        "bars_seen": 300,
        "last_close": 100.0,
        "sma": {"20": 102.0, "50": 105.0},
        "ema": {"12": 100.5, "26": 102.0},
        "rsi14": 28.0,
        "macd": {"macd": -1.0, "signal": -0.5, "hist": -0.5},
        "bbands": {
            "mid": 102.0,
            "upper": 108.0,
            "lower": 96.0,
            "width_pct": 11.7,
            "position": 0.10,  # close to lower band
        },
        "atr14": 1.5,
    }
    base.update(overrides)
    return base


def test_strategy_returns_buy_on_oversold_mean_reversion():
    d = strategy.evaluate("AAPL", _ready_indicators())
    assert d.action == "BUY", d
    assert d.confidence >= 0.43  # doctrine floor
    assert d.target_price is not None and d.target_price > 100.0
    assert d.stop_price is not None and d.stop_price < 100.0
    assert d.evidence.get("doctrine") == "mean_reversion"
    assert d.evidence.get("doctrine_version") == "barracuda_native_v1"


def test_strategy_holds_when_indicators_not_ready():
    d = strategy.evaluate("AAPL", {"ready": False})
    assert d.action == "HOLD"
    assert d.skipped_reason == "indicators_not_ready"


def test_strategy_holds_on_missing_indicators():
    d = strategy.evaluate("AAPL", {"ready": True, "last_close": 100.0})
    assert d.action == "HOLD"
    assert d.skipped_reason is not None
    assert d.skipped_reason.startswith("missing_indicators:")


def test_strategy_holds_when_signal_too_weak():
    # RSI=50, BB position=0.5 → zero signal → HOLD
    ind = _ready_indicators(rsi14=50.0, bbands={
        "mid": 102.0, "upper": 108.0, "lower": 96.0,
        "width_pct": 11.7, "position": 0.5,
    })
    d = strategy.evaluate("AAPL", ind)
    assert d.action == "HOLD"
    assert d.skipped_reason == "no_mean_reversion_signal"


def test_strategy_holds_when_below_50sma_floor():
    # Oversold but price has crashed > 8% below 50-SMA → trend filter
    # rejects (in_buy_trend=False).
    ind = _ready_indicators(last_close=80.0, sma={"20": 102.0, "50": 105.0})
    d = strategy.evaluate("AAPL", ind)
    assert d.action == "HOLD"


def test_strategy_short_disabled_by_default(monkeypatch):
    # Overbought signal — should still HOLD because shorts are off.
    monkeypatch.delenv("BARRACUDA_SHORTS_ENABLED", raising=False)
    ind = _ready_indicators(
        rsi14=78.0,
        bbands={
            "mid": 102.0, "upper": 108.0, "lower": 96.0,
            "width_pct": 11.7, "position": 0.92,
        },
    )
    d = strategy.evaluate("AAPL", ind)
    assert d.action == "HOLD", "shorts must be OFF by default"


def test_strategy_short_when_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("BARRACUDA_SHORTS_ENABLED", "true")
    # Overbought: price ABOVE BB mid (target needs to be below entry).
    ind = _ready_indicators(
        last_close=107.0,
        rsi14=78.0,
        sma={"20": 102.0, "50": 100.0},  # 50-SMA must be above 107/1.08 ≈ 99
        bbands={
            "mid": 102.0, "upper": 108.0, "lower": 96.0,
            "width_pct": 11.7, "position": 0.92,
        },
    )
    d = strategy.evaluate("AAPL", ind)
    assert d.action == "SHORT"
    assert d.confidence >= 0.43
    assert d.target_price is not None and d.target_price < 107.0
    assert d.stop_price is not None and d.stop_price > 107.0


# ────────────────────────── runner ──────────────────────────


@pytest.mark.asyncio
async def test_runner_tick_emits_to_canonical_intents(monkeypatch):
    """End-to-end: seed a universe doc + an oversold snapshot, then
    confirm `tick_once` writes a `shared_intents` row via the canonical
    path (NOT via HTTP). Pre-cleans by a unique test symbol so the
    real intents queue isn't polluted across reruns.
    """
    monkeypatch.setenv("AUTO_DRY_RUN_ON_INGEST", "false")  # keep test deterministic
    from db import db

    test_symbol = f"BAR{uuid.uuid4().hex[:6].upper()}"
    try:
        # 1. Seed universe entry (equity)
        await db["patterns_universe"].insert_one({
            "symbol": test_symbol, "lane": "equity",
        })
        # 2. Seed oversold indicator snapshot
        await db["shared_indicator_snapshots"].insert_one({
            "symbol": test_symbol,
            "source": "test",
            "tf": "1h",
            "computed_at": "2026-02-23T12:00:00Z",
            "indicators": _ready_indicators(),
        })

        summary = await runner.tick_once(db)

        assert summary["universe_size"] >= 1
        emitted_symbols = {row["symbol"] for row in summary.get("emitted", [])}
        assert test_symbol in emitted_symbols, summary

        # Confirm canonical intent row landed in shared_intents
        intent_doc = await db["shared_intents"].find_one(
            {"symbol": test_symbol, "stack": "barracuda"},
        )
        assert intent_doc is not None
        assert intent_doc["stack_canonical"] == "barracuda"
        assert intent_doc["action"] == "BUY"
        assert intent_doc["lane"] == "equity"
        assert intent_doc["evidence"]["emit_source"] == "barracuda_native_runtime"
        # Doctrine doctrine sidecar packet attached — proves we went
        # through `_post_intent_impl`, not a custom shortcut path.
        assert "doctrine_packet" in intent_doc

        # And a tick-row summary was persisted
        tick_row = await db["barracuda_native_runtime_ticks"].find_one(
            sort=[("started_at", -1)],
        )
        assert tick_row is not None
        assert tick_row["runtime"] == "barracuda_native_v1"
    finally:
        # Cleanup
        await db["patterns_universe"].delete_many({"symbol": test_symbol})
        await db["shared_indicator_snapshots"].delete_many({"symbol": test_symbol})
        await db["shared_intents"].delete_many({"symbol": test_symbol})


@pytest.mark.asyncio
async def test_runner_records_no_snapshot_symbols(monkeypatch):
    """A universe symbol without an indicator snapshot must be reported
    via `no_snapshot_symbols`, not silently dropped."""
    monkeypatch.setenv("AUTO_DRY_RUN_ON_INGEST", "false")
    from db import db

    test_symbol = f"NOSNP{uuid.uuid4().hex[:6].upper()}"
    try:
        await db["patterns_universe"].insert_one({
            "symbol": test_symbol, "lane": "equity",
        })
        summary = await runner.tick_once(db)
        assert test_symbol in summary["no_snapshot_symbols"]
        assert summary["no_snapshot_count"] >= 1
    finally:
        await db["patterns_universe"].delete_many({"symbol": test_symbol})


# ────────────────────────── scheduler ──────────────────────────


def test_scheduler_disabled_by_default(monkeypatch):
    monkeypatch.delenv("BARRACUDA_NATIVE_RUNTIME_ENABLED", raising=False)
    assert barracuda_runtime.is_enabled() is False


def test_scheduler_enabled_via_env(monkeypatch):
    monkeypatch.setenv("BARRACUDA_NATIVE_RUNTIME_ENABLED", "true")
    assert barracuda_runtime.is_enabled() is True


def test_scheduler_start_worker_is_noop_when_disabled(monkeypatch):
    """Lifespan must NEVER spawn the loop when the flag is off."""
    monkeypatch.delenv("BARRACUDA_NATIVE_RUNTIME_ENABLED", raising=False)
    barracuda_runtime.start_worker()
    # Public surface: when disabled, the task never gets created.
    sched = barracuda_runtime._instance()  # type: ignore[attr-defined]
    assert sched.task is None


# ────────────────────────── dedup with neutral_brains ──────────────────────────


def test_neutral_brains_skips_barracuda_when_native_runtime_enabled(monkeypatch):
    """When the operator flips the native flag, the legacy
    `external/brains/runner.py` MUST drop Barracuda so we don't double-
    emit. This test is a static-scan against the runner source — the
    actual takeover branch is exercised at start_neutral_brains time
    on the real boot path; we just verify the contract is in the code.
    """
    src = open("/app/external/brains/runner.py", "r").read()
    assert "BARRACUDA_NATIVE_RUNTIME_ENABLED" in src
    # New shape (2026-02-23): generic native-takeover dedup loop —
    # the per-brain wording was replaced when the migration extended
    # to all four brains.
    assert "_native_takeover_active" in src
    assert "native takeover" in src.lower()
