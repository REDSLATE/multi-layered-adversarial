"""Tests for the D (dissent) + E (brain-accuracy) endpoints.

These are read-only aggregators over `trader_receipts` + `executions`
in the local SQLite store, so we drive them by seeding the store
directly — no HTTP, no Mongo, no live trader loop.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")

from trader import audit, store  # noqa: E402


@pytest.fixture()
def fresh_store(tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    store.init(
        str(tmp_path / "executions.sqlite"),
        str(tmp_path / "jsonl"),
    )
    yield tmp_path
    store.close()
    loop.close()
    asyncio.set_event_loop(None)


async def _seed_cycle(
    *, cycle_id: str, executor_brain: str, executor_verdict: str,
    signals: list, ts: str = "2026-07-02T12:00:00+00:00",
    lane: str = "equity", symbol: str = "TSLA",
    quote: dict | None = None,
):
    """Write one receipt row with the shape the endpoints consume.
    `chosen.confidence` is taken from the executor's own signal so
    per-cycle avg_confidence math is realistic."""
    exec_sig = next(
        (s for s in signals if s.get("brain") == executor_brain), {},
    )
    conf = exec_sig.get("confidence", 0.72)
    await audit.write_receipt(
        db=None,
        cycle_id=cycle_id, lane=lane, symbol=symbol,
        last_price=(quote or {}).get("last_price", 425.0),
        signals=signals,
        chosen={"brain": executor_brain, "verdict": executor_verdict,
                "confidence": conf},
        seats={"executor": executor_brain},
        angels={"executor": "test"},
        risk_verdict={"ok": True, "reason": "ok"},
        quote=quote,
    )
    # Force ts to a stable value so the window filter is deterministic
    with store._lock:
        store._require_conn().execute(
            "UPDATE trader_receipts SET ts=? WHERE cycle_id=?",
            (ts, cycle_id),
        )


# ─── D — dissent aggregator ───────────────────────────────────────

@pytest.mark.asyncio
async def test_dissent_counts_disagreements_correctly(fresh_store):
    """Two cycles: Camino executes BUY; Barracuda dissents once,
    then agrees once. Expected dissent rate for Barracuda = 50%."""
    from backend.routes import admin_trader
    # Import-time side-effect: `_import_trader` sets sys.path
    admin_trader._import_trader()

    await _seed_cycle(
        cycle_id="c1", executor_brain="camino", executor_verdict="BUY",
        signals=[
            {"brain": "camino", "verdict": "BUY", "confidence": 0.72},
            {"brain": "barracuda", "verdict": "HOLD", "confidence": 0.4},
        ],
    )
    await _seed_cycle(
        cycle_id="c2", executor_brain="camino", executor_verdict="BUY",
        signals=[
            {"brain": "camino", "verdict": "BUY", "confidence": 0.8},
            {"brain": "barracuda", "verdict": "BUY", "confidence": 0.55},
        ],
    )

    # Call the endpoint's handler function directly (bypass HTTP auth).
    result = await admin_trader.trader_dissent(_={}, window_hours=24, lane=None)
    brains = {b["brain"]: b for b in result["brains"]}
    assert brains["barracuda"]["cycles"] == 2
    assert brains["barracuda"]["dissents"] == 1
    assert brains["barracuda"]["dissent_rate_pct"] == 50.0
    # Camino should have 0 dissents (it's the executor)
    assert brains["camino"]["dissents"] == 0


@pytest.mark.asyncio
async def test_dissent_tracks_who_you_dissent_against(fresh_store):
    """`top_dissents_vs` should surface which executor a brain most
    frequently disagrees with."""
    from backend.routes import admin_trader
    admin_trader._import_trader()

    # 3 cycles where hellcat dissents against camino,
    # 1 cycle where hellcat dissents against barracuda
    for i in range(3):
        await _seed_cycle(
            cycle_id=f"c-cam-{i}",
            executor_brain="camino", executor_verdict="BUY",
            signals=[
                {"brain": "camino", "verdict": "BUY", "confidence": 0.7},
                {"brain": "hellcat", "verdict": "SELL", "confidence": 0.7},
            ],
        )
    await _seed_cycle(
        cycle_id="c-bar-0",
        executor_brain="barracuda", executor_verdict="SELL",
        signals=[
            {"brain": "barracuda", "verdict": "SELL", "confidence": 0.7},
            {"brain": "hellcat", "verdict": "BUY", "confidence": 0.7},
        ],
    )
    result = await admin_trader.trader_dissent(_={}, window_hours=24, lane=None)
    hellcat = next(b for b in result["brains"] if b["brain"] == "hellcat")
    assert hellcat["dissents"] == 4
    assert hellcat["top_dissents_vs"]["camino"] == 3
    assert hellcat["top_dissents_vs"]["barracuda"] == 1


# ─── E — brain-accuracy aggregator ────────────────────────────────

@pytest.mark.asyncio
async def test_brain_accuracy_reports_fires_and_fills(fresh_store):
    """Two Camino fires: one filled, one broker-errored. Fill rate 50%."""
    from backend.routes import admin_trader
    admin_trader._import_trader()

    # cycle c-fire-1: Camino fires and broker fills
    await _seed_cycle(
        cycle_id="c-fire-1",
        executor_brain="camino", executor_verdict="BUY",
        signals=[
            {"brain": "camino", "verdict": "BUY", "confidence": 0.72},
        ],
        quote={
            "quote_source": "webull_mqtt", "quote_age_ms": 42,
            "bid": 425.0, "ask": 425.05, "spread_bps": 1.18,
            "last_price": 425.02, "l1_stale": False,
        },
    )
    store.record_execution({
        "intent_id": "trader-c-fire-1-equity",
        "ts": "2026-07-02T12:00:01+00:00",
        "brain": "camino", "lane": "equity", "action": "BUY",
        "symbol": "TSLA", "notional_usd": 5.0,
        "ok": True, "broker": "webull", "broker_order_id": "wb-1",
    })
    # cycle c-fire-2: Camino fires but broker errors
    await _seed_cycle(
        cycle_id="c-fire-2",
        executor_brain="camino", executor_verdict="BUY",
        signals=[
            {"brain": "camino", "verdict": "BUY", "confidence": 0.70},
        ],
        quote={
            "quote_source": "webull_mqtt", "quote_age_ms": 55,
            "bid": 425.1, "ask": 425.15, "spread_bps": 1.18,
            "last_price": 425.12, "l1_stale": False,
        },
    )
    store.record_execution({
        "intent_id": "trader-c-fire-2-equity",
        "ts": "2026-07-02T12:00:02+00:00",
        "brain": "camino", "lane": "equity", "action": "BUY",
        "symbol": "TSLA", "notional_usd": 5.0,
        "ok": False,
        "exception_type": "BrokerError",
        "exception_msg": "insufficient funds",
    })

    result = await admin_trader.trader_brain_accuracy(_={}, window_hours=24, lane=None)
    cam = next((b for b in result["brains"] if b["brain"] == "camino"), None)
    assert cam is not None
    assert cam["fires"] == 2
    assert cam["fills"] == 1
    assert cam["fill_rate_pct"] == 50.0
    assert cam["broker_error_rate_pct"] == 50.0
    assert cam["avg_confidence"] == pytest.approx(0.71, abs=0.01)
    assert cam["avg_spread_bps_at_fire"] == pytest.approx(1.18, abs=0.01)
    assert cam["avg_quote_age_ms_at_fire"] == pytest.approx(48.5, abs=1)


@pytest.mark.asyncio
async def test_brain_accuracy_ignores_holds_and_no_signal_cycles(fresh_store):
    """A cycle with no executor chosen (or a HOLD) must not inflate
    the fires count — we only count actual attempts to trade."""
    from backend.routes import admin_trader
    admin_trader._import_trader()

    await _seed_cycle(
        cycle_id="c-hold",
        executor_brain="camino", executor_verdict="HOLD",
        signals=[{"brain": "camino", "verdict": "HOLD",
                  "confidence": 0.2}],
    )
    # No execution recorded for HOLDs — they never reach the fire path
    result = await admin_trader.trader_brain_accuracy(_={}, window_hours=24, lane=None)
    cam = next((b for b in result["brains"] if b["brain"] == "camino"), None)
    # camino never fired → not in the output at all
    assert cam is None or cam["fires"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
