"""Tests for `shared.promotion_artifact_report` — the shadow-vs-fill
PromotionArtifact evidence builder.

Covers:
  * pure helpers (`_action_to_direction`, `_direction_signed_return`,
    `_verdict_from_metrics`)
  * `compute_brain_report` against a seeded DB:
      - empty data
      - mixed agreement (3 of 5 directional matches, 2 of 4 MTM hits)
      - high agreement (5 of 5 matches, 4 of 4 MTM hits → recommend_promote)
  * HTTP endpoints (`/api/admin/promotion-artifact/{brain}` and `""`)
    require auth (401 without token) and return shape contract.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import requests

from db import db
from namespaces import (
    EXECUTION_RECEIPTS,
    SHARED_INTENTS,
    SHARED_OHLCV_BARS,
)
from shared.promotion_artifact_report import (
    DIRECTIONAL_AGREEMENT_WINDOW_MIN,
    HIT_RATE_HORIZON_MIN,
    MIN_SAMPLES_FOR_VERDICT,
    PROMOTION_AGREEMENT_FLOOR,
    PROMOTION_HIT_RATE_FLOOR,
    SIMULATED_NOTIONAL_USD,
    _action_to_direction,
    _direction_signed_return,
    _verdict_from_metrics,
    compute_brain_report,
)


# ───────────────────── pure helpers ─────────────────────────────────


def test_action_to_direction():
    assert _action_to_direction("BUY") == "long"
    assert _action_to_direction("enter_long") == "long"
    assert _action_to_direction("SCALE_IN") == "long"
    assert _action_to_direction("SELL") == "short"
    assert _action_to_direction("EXIT") == "short"
    assert _action_to_direction("enter_short") == "short"
    assert _action_to_direction("HOLD") is None
    assert _action_to_direction(None) is None
    assert _action_to_direction("") is None


def test_direction_signed_return_long_profit():
    # long, price went up → positive
    assert _direction_signed_return("long", 100.0, 110.0) == pytest.approx(0.10)


def test_direction_signed_return_long_loss():
    assert _direction_signed_return("long", 100.0, 90.0) == pytest.approx(-0.10)


def test_direction_signed_return_short_profit():
    # short, price went down → positive
    assert _direction_signed_return("short", 100.0, 90.0) == pytest.approx(0.10)


def test_direction_signed_return_zero_entry_guard():
    assert _direction_signed_return("long", 0.0, 100.0) == 0.0


def test_verdict_insufficient_data_low_samples():
    assert _verdict_from_metrics(samples=5, hit_rate=1.0, agreement=1.0) == "insufficient_data"


def test_verdict_insufficient_data_missing_metrics():
    assert _verdict_from_metrics(samples=50, hit_rate=None, agreement=0.9) == "insufficient_data"


def test_verdict_keep_in_challenger():
    # 25% hit-rate is below 30% floor
    assert (
        _verdict_from_metrics(samples=MIN_SAMPLES_FOR_VERDICT, hit_rate=0.25, agreement=0.9)
        == "keep_in_challenger"
    )


def test_verdict_recommend_promote_at_threshold():
    assert (
        _verdict_from_metrics(
            samples=MIN_SAMPLES_FOR_VERDICT,
            hit_rate=PROMOTION_HIT_RATE_FLOOR,
            agreement=PROMOTION_AGREEMENT_FLOOR,
        )
        == "recommend_promote"
    )


# ───────────────────── compute_brain_report (DB-backed) ──────────────


TEST_BRAIN = "_pa_test_brain"  # synthetic — isolates seeded test data from live camaro intents
TEST_BENCHMARK = "_pa_test_benchmark"
TEST_SYMBOL_A = "PRTEST_A"  # use unique synthetic symbols to avoid clashing with prod feeders
TEST_SYMBOL_B = "PRTEST_B"


async def _wipe_test_data():
    await db[SHARED_INTENTS].delete_many({"stack": {"$in": [TEST_BRAIN, TEST_BENCHMARK]}})
    await db[EXECUTION_RECEIPTS].delete_many({"stack": {"$in": [TEST_BRAIN, TEST_BENCHMARK]}})
    await db[SHARED_OHLCV_BARS].delete_many({"symbol": {"$regex": f"^{TEST_SYMBOL_A[:6]}"}})
    await db[SHARED_OHLCV_BARS].delete_many({"symbol": {"$regex": f"^{TEST_SYMBOL_B[:6]}"}})


async def _seed_intent(*, brain: str, symbol: str, action: str, ts: datetime, intent_id: str):
    await db[SHARED_INTENTS].insert_one({
        "intent_id": intent_id,
        "stack": brain,
        "symbol": symbol,
        "action": action,
        "confidence": 0.7,
        "lane": "equity",
        "ingest_ts": ts.isoformat(),
        "holds_executor_seat": False,
        "executor_holder_at_post": TEST_BENCHMARK,
        "may_execute": False,
        "requires_gate_pass": True,
    })


async def _seed_receipt(*, brain: str, symbol: str, action: str, price: float, ts: datetime, notional: float = 1000.0):
    await db[EXECUTION_RECEIPTS].insert_one({
        "receipt_id": f"rcpt-{symbol}-{ts.isoformat()}",
        "stack": brain,
        "symbol": symbol,
        "action": action,
        "side": "buy" if action.upper() in ("BUY", "ENTER_LONG") else "sell",
        "notional_usd": notional,
        "filled_avg_price": price,
        "executed_at": ts.isoformat(),
        "status": "filled",
    })


async def _seed_bar(*, symbol: str, ts: datetime, close: float):
    await db[SHARED_OHLCV_BARS].update_one(
        {"source": "manual", "symbol": symbol, "tf": "1m", "ts": ts.isoformat()},
        {"$set": {
            "source": "manual", "symbol": symbol, "tf": "1m",
            "ts": ts.isoformat(),
            "o": close, "h": close, "l": close, "c": close, "v": 0.0,
        }},
        upsert=True,
    )


@pytest.mark.asyncio
async def test_empty_data_returns_insufficient_data():
    await _wipe_test_data()
    rep = await compute_brain_report(brain=TEST_BRAIN, hours=1, benchmark_brain=TEST_BENCHMARK)
    assert rep["brain"] == TEST_BRAIN
    assert rep["benchmark_brain"] == TEST_BENCHMARK
    assert rep["metrics"]["sample_size"] == 0
    assert rep["metrics"]["directional_agreement_rate"] is None
    assert rep["metrics"]["hit_rate_mtm"] is None
    assert rep["verdict"] == "insufficient_data"
    assert "0 shadow proposals" in rep["verdict_rationale"]
    assert rep["per_intent"] == []
    assert "thresholds" in rep
    assert rep["report_version"].startswith("promotion_artifact_v1")


@pytest.mark.asyncio
async def test_high_agreement_recommends_promote():
    """Seed N >= MIN_SAMPLES shadow BUYs that all match Alpha's BUYs and
    all move up over the horizon — should recommend promote."""
    await _wipe_test_data()
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    n = MIN_SAMPLES_FOR_VERDICT
    for i in range(n):
        ts = base + timedelta(minutes=i * 3)
        await _seed_intent(
            brain=TEST_BRAIN,
            symbol=TEST_SYMBOL_A,
            action="BUY",
            ts=ts,
            intent_id=f"hi-{i}",
        )
        # Alpha fills the same direction within ±DIRECTIONAL window
        await _seed_receipt(
            brain=TEST_BENCHMARK,
            symbol=TEST_SYMBOL_A,
            action="BUY",
            price=100.0,
            ts=ts + timedelta(minutes=1),
        )
        # Entry bar
        await _seed_bar(symbol=TEST_SYMBOL_A, ts=ts, close=100.0)
        # Horizon bar — price moved up
        await _seed_bar(symbol=TEST_SYMBOL_A, ts=ts + timedelta(minutes=HIT_RATE_HORIZON_MIN), close=110.0)

    rep = await compute_brain_report(brain=TEST_BRAIN, hours=24, benchmark_brain=TEST_BENCHMARK)
    assert rep["metrics"]["sample_size"] == n
    assert rep["metrics"]["directional_agreement_hits"] == n
    assert rep["metrics"]["directional_agreement_rate"] == pytest.approx(1.0)
    assert rep["metrics"]["hit_rate_eligible"] == n
    assert rep["metrics"]["hit_rate_hits"] == n
    assert rep["metrics"]["hit_rate_mtm"] == pytest.approx(1.0)
    # Each intent: +10% MTM * $1000 notional = $100 → $100 * n
    assert rep["metrics"]["simulated_pnl_usd"] == pytest.approx(100.0 * n, abs=0.5)
    assert rep["verdict"] == "recommend_promote"
    assert "evidence supports promotion" in rep["verdict_rationale"]


@pytest.mark.asyncio
async def test_mixed_data_keep_in_challenger():
    """Seed N shadow intents, half of which agree with Alpha and half of
    which move favorably — both metrics land BELOW the 30% floor by design.
    """
    await _wipe_test_data()
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    n = MIN_SAMPLES_FOR_VERDICT  # need at least min samples for a non-"insufficient" verdict
    # Engineer 20% agreement, 20% hit-rate. Each intent uses a DISTINCT
    # symbol so the directional-agreement window can't bleed across
    # adjacent intents' receipts.
    seeded_symbols = []
    for i in range(n):
        ts = base + timedelta(minutes=i * 3)
        agrees = (i % 5 == 0)        # 1 in 5 → 20%
        moves_up = (i % 5 == 0)      # 1 in 5 → 20%
        sym = f"{TEST_SYMBOL_B}_{i}"
        seeded_symbols.append(sym)
        await _seed_intent(
            brain=TEST_BRAIN,
            symbol=sym,
            action="BUY",
            ts=ts,
            intent_id=f"mx-{i}",
        )
        if agrees:
            await _seed_receipt(
                brain=TEST_BENCHMARK,
                symbol=sym,
                action="BUY",
                price=100.0,
                ts=ts + timedelta(minutes=2),
            )
        else:
            # Alpha SELLs (opposite) — should NOT count as agreement
            await _seed_receipt(
                brain=TEST_BENCHMARK,
                symbol=sym,
                action="SELL",
                price=100.0,
                ts=ts + timedelta(minutes=2),
            )
        await _seed_bar(symbol=sym, ts=ts, close=100.0)
        horizon_close = 110.0 if moves_up else 95.0
        await _seed_bar(
            symbol=sym,
            ts=ts + timedelta(minutes=HIT_RATE_HORIZON_MIN),
            close=horizon_close,
        )

    rep = await compute_brain_report(brain=TEST_BRAIN, hours=24, benchmark_brain=TEST_BENCHMARK)
    assert rep["metrics"]["sample_size"] == n
    assert rep["metrics"]["directional_agreement_rate"] == pytest.approx(0.20, abs=0.01)
    assert rep["metrics"]["hit_rate_mtm"] == pytest.approx(0.20, abs=0.01)
    # 20% < 30% floor → keep in challenger
    assert rep["verdict"] == "keep_in_challenger"
    assert "below" in rep["verdict_rationale"]


@pytest.mark.asyncio
async def test_exec_seat_intents_excluded():
    """Intents where the brain HOLDS the executor seat are NOT shadow
    proposals and must be excluded from the report's sample."""
    await _wipe_test_data()
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    await db[SHARED_INTENTS].insert_one({
        "intent_id": "executor-seat-row",
        "stack": TEST_BRAIN,
        "symbol": TEST_SYMBOL_A,
        "action": "BUY",
        "ingest_ts": base.isoformat(),
        "holds_executor_seat": True,
        "lane": "equity",
        "confidence": 0.7,
    })
    rep = await compute_brain_report(brain=TEST_BRAIN, hours=24, benchmark_brain=TEST_BENCHMARK)
    assert rep["metrics"]["sample_size"] == 0
    await _wipe_test_data()


# ─────────────────────── HTTP layer ───────────────────────────────────


def test_endpoint_requires_auth(base_url):
    r = requests.get(f"{base_url}/api/admin/promotion-artifact/camaro", timeout=15)
    assert r.status_code in (401, 403)


def test_endpoint_unknown_brain_404(auth_client, base_url):
    r = auth_client.get(f"{base_url}/api/admin/promotion-artifact/not_a_brain?hours=1", timeout=15)
    assert r.status_code == 404


def test_endpoint_brain_equals_benchmark_400(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/promotion-artifact/alpha?benchmark_brain=alpha&hours=1",
        timeout=15,
    )
    assert r.status_code == 400


def test_endpoint_returns_shape(auth_client, base_url):
    r = auth_client.get(f"{base_url}/api/admin/promotion-artifact/camaro?hours=1", timeout=20)
    assert r.status_code == 200
    body = r.json()
    for k in ("brain", "benchmark_brain", "window", "thresholds", "metrics",
              "verdict", "verdict_rationale", "per_intent", "generated_at",
              "report_version"):
        assert k in body, f"missing key: {k}"
    assert body["brain"] == "camaro"
    assert body["thresholds"]["min_samples"] == MIN_SAMPLES_FOR_VERDICT
    assert body["thresholds"]["hit_rate_floor"] == PROMOTION_HIT_RATE_FLOOR
    assert body["thresholds"]["agreement_floor"] == PROMOTION_AGREEMENT_FLOOR
    assert body["thresholds"]["simulated_notional_usd"] == SIMULATED_NOTIONAL_USD
    assert body["thresholds"]["directional_agreement_window_min"] == DIRECTIONAL_AGREEMENT_WINDOW_MIN


def test_endpoint_all_brains(auth_client, base_url):
    r = auth_client.get(f"{base_url}/api/admin/promotion-artifact?hours=1", timeout=30)
    assert r.status_code == 200
    body = r.json()
    assert body["benchmark_brain"] == "alpha"
    # 4 runtimes - 1 benchmark = 3 reports
    assert isinstance(body["reports"], list)
    brains = {r_["brain"] for r_ in body["reports"]}
    assert "alpha" not in brains  # benchmark excluded
    assert "camaro" in brains
