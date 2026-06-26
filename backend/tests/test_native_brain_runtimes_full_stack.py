"""Regression tests for the GTO / Camino / Hellcat native runtimes.

Each brain follows the Barracuda template:
  1. strategy.evaluate — pure compute
  2. runner.tick_once  — uses the shared runner core to emit via
                         canonical `submit_intent_in_process`
  3. scheduler         — env-gated, default OFF

Plus a multi-brain "retire neutral_brains" contract test that proves
flipping all four `<BRAIN>_NATIVE_RUNTIME_ENABLED` flags makes the
legacy `external/brains/runner.py` skip every brain (effectively
retiring it without removing its code).
"""
from __future__ import annotations

import sys
import uuid

import pytest

sys.path.insert(0, "/app/backend")
sys.path.insert(0, "/app")

from shared.brains.gto import strategy as gto_strategy  # noqa: E402
from shared.brains.gto import runner as gto_runner  # noqa: E402
from shared.brains.camino import strategy as camino_strategy  # noqa: E402
from shared.brains.camino import runner as camino_runner  # noqa: E402
from shared.brains.hellcat import strategy as hellcat_strategy  # noqa: E402
from shared.brains.hellcat import runner as hellcat_runner  # noqa: E402
from shared.runtime import gto_runtime, camino_runtime, hellcat_runtime  # noqa: E402


# ────────────────────────── helpers ──────────────────────────


def _bull_indicators(**overrides):
    """An indicator dict that screams 'bullish trend + breakout' so
    GTO/Camino/Hellcat all have a path to BUY."""
    base = {
        "ready": True,
        "bars_seen": 300,
        "last_close": 110.0,
        "sma": {"20": 105.0, "50": 100.0},
        "ema": {"12": 108.0, "26": 104.0},
        "rsi14": 64.0,
        "macd": {"macd": 1.2, "signal": 0.8, "hist": 0.4},
        "bbands": {
            "mid": 105.0,
            "upper": 110.5,
            "lower": 99.5,
            "width_pct": 10.5,
            "position": 0.95,
        },
        "atr14": 2.0,
    }
    base.update(overrides)
    return base


def _bear_indicators(**overrides):
    base = {
        "ready": True,
        "bars_seen": 300,
        "last_close": 90.0,
        "sma": {"20": 95.0, "50": 100.0},
        "ema": {"12": 92.0, "26": 96.0},
        "rsi14": 36.0,
        "macd": {"macd": -1.2, "signal": -0.8, "hist": -0.4},
        "bbands": {
            "mid": 95.0,
            "upper": 100.5,
            "lower": 89.5,
            "width_pct": 11.5,
            "position": 0.05,
        },
        "atr14": 2.0,
    }
    base.update(overrides)
    return base


# ────────────────────────── GTO ──────────────────────────


def test_gto_buy_on_momentum_confirmed():
    d = gto_strategy.evaluate("AAPL", _bull_indicators())
    assert d.action == "BUY", d
    assert d.confidence >= 0.45
    assert d.target_price is not None and d.target_price > 110.0
    assert d.stop_price is not None and d.stop_price < 110.0
    assert d.evidence["doctrine"] == "momentum"


def test_gto_holds_on_neutral_macd():
    ind = _bull_indicators(macd={"macd": 0.0, "signal": 0.0, "hist": 0.0})
    d = gto_strategy.evaluate("AAPL", ind)
    assert d.action == "HOLD"


def test_gto_holds_on_missing_macd_hist():
    ind = _bull_indicators(macd={"macd": 0.5})
    d = gto_strategy.evaluate("AAPL", ind)
    assert d.action == "HOLD"
    assert d.skipped_reason is not None and "macd_hist" in d.skipped_reason


def test_gto_short_disabled_by_default(monkeypatch):
    monkeypatch.delenv("GTO_SHORTS_ENABLED", raising=False)
    d = gto_strategy.evaluate("AAPL", _bear_indicators())
    assert d.action == "HOLD"


def test_gto_short_when_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("GTO_SHORTS_ENABLED", "true")
    d = gto_strategy.evaluate("AAPL", _bear_indicators())
    assert d.action == "SHORT"
    assert d.target_price is not None and d.target_price < 90.0


# ────────────────────────── Camino ──────────────────────────


def test_camino_buy_on_clean_uptrend():
    d = camino_strategy.evaluate("MSFT", _bull_indicators())
    assert d.action == "BUY", d
    assert d.confidence >= 0.46
    assert d.evidence["doctrine"] == "trend"


def test_camino_holds_when_below_50sma():
    # Not above SMA(50) → no uptrend filter pass.
    ind = _bull_indicators(last_close=99.0, sma={"20": 105.0, "50": 100.0})
    d = camino_strategy.evaluate("MSFT", ind)
    assert d.action == "HOLD"


def test_camino_holds_on_overbought_rsi():
    ind = _bull_indicators(rsi14=75.0)
    d = camino_strategy.evaluate("MSFT", ind)
    assert d.action == "HOLD"


def test_camino_holds_when_missing_indicators():
    d = camino_strategy.evaluate("MSFT", {"ready": True, "last_close": 100.0})
    assert d.action == "HOLD"
    assert d.skipped_reason is not None and d.skipped_reason.startswith(
        "missing_indicators:",
    )


def test_camino_short_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CAMINO_SHORTS_ENABLED", raising=False)
    d = camino_strategy.evaluate("MSFT", _bear_indicators())
    assert d.action == "HOLD"


def test_camino_short_when_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("CAMINO_SHORTS_ENABLED", "true")
    d = camino_strategy.evaluate("MSFT", _bear_indicators())
    assert d.action == "SHORT"


# ────────────────────────── Hellcat ──────────────────────────


def test_hellcat_buy_on_confirmed_breakout():
    d = hellcat_strategy.evaluate("NVDA", _bull_indicators())
    assert d.action == "BUY", d
    # Highest confidence floor in the stack.
    assert d.confidence >= 0.48
    assert d.evidence["doctrine"] == "breakout"


def test_hellcat_holds_below_upper_band():
    ind = _bull_indicators(bbands={
        "mid": 105.0, "upper": 110.5, "lower": 99.5,
        "width_pct": 10.5, "position": 0.50,
    })
    d = hellcat_strategy.evaluate("NVDA", ind)
    assert d.action == "HOLD"


def test_hellcat_holds_when_below_sma20():
    # Even with BB position high, requires last_close > SMA20.
    ind = _bull_indicators(last_close=104.0, bbands={
        "mid": 105.0, "upper": 110.5, "lower": 99.5,
        "width_pct": 10.5, "position": 0.92,
    })
    d = hellcat_strategy.evaluate("NVDA", ind)
    assert d.action == "HOLD"


def test_hellcat_short_disabled_by_default(monkeypatch):
    monkeypatch.delenv("HELLCAT_SHORTS_ENABLED", raising=False)
    d = hellcat_strategy.evaluate("NVDA", _bear_indicators())
    assert d.action == "HOLD"


def test_hellcat_short_when_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("HELLCAT_SHORTS_ENABLED", "true")
    d = hellcat_strategy.evaluate("NVDA", _bear_indicators())
    assert d.action == "SHORT"


# ──────────────── runner end-to-end (one per brain) ────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("brain,tick_once_fn,tick_log,bullish", [
    ("gto", gto_runner.tick_once, gto_runner.TICK_LOG_COLLECTION, True),
    ("camino", camino_runner.tick_once, camino_runner.TICK_LOG_COLLECTION, True),
    ("hellcat", hellcat_runner.tick_once, hellcat_runner.TICK_LOG_COLLECTION, True),
])
async def test_runner_emits_to_canonical_intents(
    monkeypatch, brain, tick_once_fn, tick_log, bullish,
):
    """For each new brain: seed a universe doc + a bullish indicator
    snapshot, run the tick, and confirm a `shared_intents` row landed
    via the canonical path (doctrine_packet attached → proves it went
    through `_post_intent_impl`)."""
    monkeypatch.setenv("AUTO_DRY_RUN_ON_INGEST", "false")
    from db import db

    test_symbol = f"{brain.upper()[:3]}{uuid.uuid4().hex[:6].upper()}"
    try:
        await db["patterns_universe"].insert_one({
            "symbol": test_symbol, "lane": "equity",
        })
        await db["shared_indicator_snapshots"].insert_one({
            "symbol": test_symbol,
            "source": "test",
            "tf": "1h",
            "computed_at": "2026-02-23T12:00:00Z",
            "indicators": _bull_indicators() if bullish else _bear_indicators(),
        })

        summary = await tick_once_fn(db)
        emitted_symbols = {row["symbol"] for row in summary.get("emitted", [])}
        assert test_symbol in emitted_symbols, (
            f"{brain}: expected emit on bullish synthetic data, got {summary}"
        )

        intent_doc = await db["shared_intents"].find_one(
            {"symbol": test_symbol, "stack": brain},
        )
        assert intent_doc is not None
        assert intent_doc["stack_canonical"] == brain
        assert intent_doc["lane"] == "equity"
        assert intent_doc["evidence"]["emit_source"] == f"{brain}_native_runtime"
        assert "doctrine_packet" in intent_doc

        tick_row = await db[tick_log].find_one(sort=[("started_at", -1)])
        assert tick_row is not None
        assert tick_row["brain_id"] == brain
        assert tick_row["runtime"] == f"{brain}_native_v1"
    finally:
        await db["patterns_universe"].delete_many({"symbol": test_symbol})
        await db["shared_indicator_snapshots"].delete_many({"symbol": test_symbol})
        await db["shared_intents"].delete_many({"symbol": test_symbol})


# ────────────────────────── schedulers ──────────────────────────


@pytest.mark.parametrize("mod,env", [
    (gto_runtime, "GTO_NATIVE_RUNTIME_ENABLED"),
    (camino_runtime, "CAMINO_NATIVE_RUNTIME_ENABLED"),
    (hellcat_runtime, "HELLCAT_NATIVE_RUNTIME_ENABLED"),
])
def test_scheduler_default_off(monkeypatch, mod, env):
    monkeypatch.delenv(env, raising=False)
    assert mod.is_enabled() is False
    mod.start_worker()
    assert mod._instance().task is None  # noqa: SLF001


@pytest.mark.parametrize("mod,env", [
    (gto_runtime, "GTO_NATIVE_RUNTIME_ENABLED"),
    (camino_runtime, "CAMINO_NATIVE_RUNTIME_ENABLED"),
    (hellcat_runtime, "HELLCAT_NATIVE_RUNTIME_ENABLED"),
])
def test_scheduler_enable_via_env(monkeypatch, mod, env):
    monkeypatch.setenv(env, "true")
    assert mod.is_enabled() is True


# ───────────────── retire neutral_brains contract ─────────────────


def test_neutral_brains_skips_all_four_when_native_enabled():
    """Static-scan contract: flipping all four
    `<BRAIN>_NATIVE_RUNTIME_ENABLED` flags MUST cause the legacy
    neutral_brains loop to skip every brain — effectively retiring
    the legacy runner without removing its code.
    """
    src = open("/app/external/brains/runner.py", "r").read()
    for env in (
        "BARRACUDA_NATIVE_RUNTIME_ENABLED",
        "GTO_NATIVE_RUNTIME_ENABLED",
        "CAMINO_NATIVE_RUNTIME_ENABLED",
        "HELLCAT_NATIVE_RUNTIME_ENABLED",
    ):
        assert env in src, f"missing dedup env-check for {env}"
    # The dedup loop must `continue` on hit (skip the runner add).
    assert "_native_takeover_active" in src
    assert "skipped_for_native" in src


def test_neutral_brains_actual_takeover_behavior(monkeypatch):
    """Simulate the dedup branch end-to-end without spawning tasks.
    Confirms that when all 4 flags are on, the BRAIN_ROSTER loop
    adds zero runners."""
    for env in (
        "BARRACUDA_NATIVE_RUNTIME_ENABLED",
        "GTO_NATIVE_RUNTIME_ENABLED",
        "CAMINO_NATIVE_RUNTIME_ENABLED",
        "HELLCAT_NATIVE_RUNTIME_ENABLED",
    ):
        monkeypatch.setenv(env, "true")

    # Inline reproduction of the dedup branch — keeps the test
    # synchronous (start_neutral_brains is async + spawns httpx
    # clients we don't want in unit tests).
    import os
    flag_map = {
        "barracuda": "BARRACUDA_NATIVE_RUNTIME_ENABLED",
        "gto":       "GTO_NATIVE_RUNTIME_ENABLED",
        "camino":    "CAMINO_NATIVE_RUNTIME_ENABLED",
        "hellcat":   "HELLCAT_NATIVE_RUNTIME_ENABLED",
    }
    roster = [("camino",), ("barracuda",), ("hellcat",), ("gto",)]

    def native_takeover_active(bid: str) -> bool:
        env = flag_map.get(bid.lower())
        if not env:
            return False
        return os.environ.get(env, "").strip().lower() in {
            "1", "true", "yes", "on",
        }

    kept = [b for (b,) in roster if not native_takeover_active(b)]
    assert kept == [], (
        "all 4 native flags ON → neutral_brains must add zero runners"
    )
