"""Regression: Hot-Brain Router DRY-RUN endpoint.

2026-06-22 P1 — Operator wants the dormant Hot-Brain Router to
become "truth serum" before being wired into live execution:

  > Would RISEDUAL have traded more?
  > Which brain is actually hot?
  > Is the Kernel helping or overblocking?
  > Are we still too conservative?

Endpoint: GET /api/admin/hot-brain-router/dry-run?days=1

This test pins:
  * Response shape (window_days, totals, by_brain, examples,
    dry_run_context, router_status)
  * The four `would_*` totals are integers and add up to
    processed-intent count
  * `examples` are spread across action buckets — the operator
    must NEVER get 20 ELEVATEs in a row
  * Read-only doctrine — no writes to `shared_intents`
  * `router_status` literally says "DORMANT" so the dashboard can't
    mistake a dry-run reading for a live wiring
  * `days` query param is validated [1, 7]
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch


@pytest.fixture
def sample_intents():
    """Synthetic intents — one per brain, mixed verdicts after
    routing. ingest_ts within the last hour so the dry-run window
    picks them up."""
    now = datetime.now(timezone.utc)
    return [
        {
            "intent_id": "i-hot-elevate",
            "stack": "gto",
            "lane": "equity",
            "symbol": "NVDA",
            "confidence": 0.78,
            "regime": "trend_up",
            "action": "BUY",
            "ingest_ts": (now - timedelta(minutes=10)).isoformat(),
        },
        {
            "intent_id": "i-cold-block",
            "stack": "hellcat",
            "lane": "equity",
            "symbol": "AMD",
            "confidence": 0.55,
            "regime": "chop",
            "action": "BUY",
            "ingest_ts": (now - timedelta(minutes=20)).isoformat(),
        },
        {
            "intent_id": "i-unknown-reduce",
            "stack": "camino",
            "lane": "crypto",
            "symbol": "BTC-USD",
            "confidence": 0.60,
            "action": "BUY",
            "ingest_ts": (now - timedelta(minutes=30)).isoformat(),
        },
        {
            "intent_id": "i-neutral-pass",
            "stack": "barracuda",
            "lane": "equity",
            "symbol": "AAPL",
            "confidence": 0.65,
            "regime": "trend_up",
            "action": "SELL",
            "ingest_ts": (now - timedelta(minutes=40)).isoformat(),
        },
        # HOLD/ABSTAIN — must be filtered OUT (action filter on the
        # endpoint) because the router only judges executable intents.
        {
            "intent_id": "i-hold-should-skip",
            "stack": "gto",
            "lane": "equity",
            "symbol": "TSLA",
            "action": "HOLD",
            "ingest_ts": (now - timedelta(minutes=5)).isoformat(),
        },
    ]


def _make_fake_db(intent_rows):
    class _Cursor:
        def __init__(self, rows):
            self._rows = rows
        def sort(self, *a, **k):
            return self
        async def to_list(self, length):
            return self._rows[:length]

    class _Coll:
        def find(self, query, projection):
            # Emulate the action filter on the endpoint —
            # routers must never judge HOLDs.
            allowed = set(query.get("action", {}).get("$in", []))
            if allowed:
                rows = [r for r in intent_rows if r.get("action") in allowed]
            else:
                rows = list(intent_rows)
            return _Cursor(rows)

    class _DB:
        def __getitem__(self, name):
            return _Coll()

    return _DB()


def _fake_perf(state: str):
    """Build a `BrainPerformance` shaped so `classify_brain` ends in
    the desired state and `route_hot_brain` returns the expected
    bucket under the dry-run's neutral RouterContext."""
    from shared.brains.hot_brain_router import BrainPerformance
    now = datetime.now(timezone.utc)
    if state == "HOT":
        return BrainPerformance(
            brain="gto", lane="equity", symbol="NVDA",
            trades=40, win_rate=0.72, avg_return_bps=85.0,
            profit_factor=2.10, max_drawdown_bps=-90.0,
            streak_wins=4, streak_losses=0,
            last_trade_at=now - timedelta(hours=2),
            lane_win_rate=0.70, symbol_win_rate=0.75,
        )
    if state == "COLD":
        return BrainPerformance(
            brain="hellcat", lane="equity", symbol="AMD",
            trades=30, win_rate=0.30, avg_return_bps=-60.0,
            profit_factor=0.40, max_drawdown_bps=-380.0,
            streak_wins=0, streak_losses=4,
            last_trade_at=now - timedelta(hours=3),
            lane_win_rate=0.32, symbol_win_rate=0.30,
        )
    if state == "NEUTRAL":
        return BrainPerformance(
            brain="barracuda", lane="equity", symbol="AAPL",
            trades=15, win_rate=0.50, avg_return_bps=5.0,
            profit_factor=1.05, max_drawdown_bps=-120.0,
            streak_wins=1, streak_losses=1,
            last_trade_at=now - timedelta(hours=4),
            lane_win_rate=0.50, symbol_win_rate=0.48,
        )
    # UNKNOWN — trades < 10 triggers REDUCE branch
    return BrainPerformance(
        brain="camino", lane="crypto", symbol="BTC-USD",
        trades=3, win_rate=0.66, avg_return_bps=20.0,
        profit_factor=1.20, max_drawdown_bps=-50.0,
        streak_wins=2, streak_losses=0,
        last_trade_at=now - timedelta(hours=1),
        lane_win_rate=0.60, symbol_win_rate=0.66,
    )


@pytest.mark.asyncio
async def test_dry_run_response_shape_and_buckets(monkeypatch, sample_intents):
    """Smoke: the four counts add up to processed intents and the
    response carries the locked schema."""
    from routes import admin_hot_brain_router as mod

    monkeypatch.setattr(mod, "db", _make_fake_db(sample_intents))

    async def fake_perf(brain, lane, symbol, lookback=20):
        return _fake_perf({
            ("gto", "equity", "NVDA"): "HOT",
            ("hellcat", "equity", "AMD"): "COLD",
            ("barracuda", "equity", "AAPL"): "NEUTRAL",
            ("camino", "crypto", "BTC-USD"): "UNKNOWN",
        }.get((brain, lane, symbol), "UNKNOWN"))

    monkeypatch.setattr(mod, "get_recent_brain_performance", fake_perf)

    resp = await mod.hot_brain_router_dry_run(days=1, _user={"sub": "test"})

    # Schema lock — all top-level keys present.
    for key in (
        "window_days", "total_intents",
        "would_block", "would_reduce", "would_pass", "would_elevate",
        "by_brain", "examples", "dry_run_context", "router_status",
    ):
        assert key in resp, f"missing top-level key {key!r} in response"

    # Window echo.
    assert resp["window_days"] == 1

    # HOLD intent must NOT be counted.
    assert resp["total_intents"] == 4, (
        f"expected 4 executable intents (HOLD filtered); got {resp['total_intents']}"
    )

    # Bucket counts sum to processed intents.
    total = (
        resp["would_block"] + resp["would_reduce"]
        + resp["would_pass"] + resp["would_elevate"]
    )
    assert total == 4, (
        f"buckets ({resp['would_block']}/{resp['would_reduce']}/"
        f"{resp['would_pass']}/{resp['would_elevate']}) must sum to "
        f"total_intents ({resp['total_intents']})"
    )

    # Router status banner — dashboard relies on the literal "DORMANT".
    assert "DORMANT" in resp["router_status"], (
        "router_status must literally say DORMANT so operators can't "
        "mistake a dry-run reading for live wiring"
    )


@pytest.mark.asyncio
async def test_dry_run_examples_spread_across_buckets(monkeypatch, sample_intents):
    """Operator must see a mix of actions in `examples`, NOT 20
    ELEVATEs in a row. We synthesize 8 HOT intents and confirm at
    most 5 ELEVATE examples are returned (per-bucket cap)."""
    from routes import admin_hot_brain_router as mod

    # 8 HOT intents on different symbols so the router emits ELEVATE 8x.
    now = datetime.now(timezone.utc)
    rows = [{
        "intent_id": f"i-{i}",
        "stack": "gto",
        "lane": "equity",
        "symbol": f"SYM{i}",
        "action": "BUY",
        "regime": "trend_up",
        "ingest_ts": (now - timedelta(minutes=i)).isoformat(),
    } for i in range(8)]

    monkeypatch.setattr(mod, "db", _make_fake_db(rows))

    async def fake_perf(brain, lane, symbol, lookback=20):
        # Always return a HOT-classified perf so every intent
        # routes to ELEVATE.
        from shared.brains.hot_brain_router import BrainPerformance
        return BrainPerformance(
            brain=brain, lane=lane, symbol=symbol,
            trades=40, win_rate=0.72, avg_return_bps=85.0,
            profit_factor=2.10, max_drawdown_bps=-90.0,
            streak_wins=4, streak_losses=0,
            last_trade_at=now - timedelta(hours=2),
            lane_win_rate=0.70, symbol_win_rate=0.75,
        )

    monkeypatch.setattr(mod, "get_recent_brain_performance", fake_perf)

    resp = await mod.hot_brain_router_dry_run(days=1, _user={"sub": "test"})

    elevate_examples = [e for e in resp["examples"]
                        if e["route_action"] == "elevate"]
    assert len(elevate_examples) <= 5, (
        f"per-bucket example cap must keep ELEVATE samples ≤ 5; "
        f"got {len(elevate_examples)} — operator would see a wall of "
        f"identical-looking rows"
    )

    # All examples carry the locked per-row schema.
    for ex in resp["examples"]:
        for key in (
            "intent_id", "brain", "symbol", "regime",
            "kernel_adjusted_score", "route_action", "reason",
        ):
            assert key in ex, f"example missing key {key!r}: {ex!r}"
        assert ex["route_action"] in {"block", "reduce", "pass", "elevate"}
        assert isinstance(ex["kernel_adjusted_score"], float)


@pytest.mark.asyncio
async def test_dry_run_validates_days_range():
    """`days` query param must be bounded [1, 7] — the operator's
    stated workflow is one trading day at a time and larger windows
    blow the response budget. Verified via the route's openapi
    metadata so the constraint can't be silently widened later."""
    from routes import admin_hot_brain_router as mod
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(mod.router)

    # Drill into the route's parameters via the OpenAPI schema —
    # FastAPI stores Query() ge/le on the field definition.
    schema = app.openapi()
    params = schema["paths"]["/admin/hot-brain-router/dry-run"]["get"]["parameters"]
    days_param = next(p for p in params if p["name"] == "days")
    constraint = days_param.get("schema", {})
    assert constraint.get("minimum") == 1, (
        f"days must enforce minimum=1; got constraint={constraint!r}"
    )
    assert constraint.get("maximum") == 7, (
        f"days must enforce maximum=7; got constraint={constraint!r}"
    )


@pytest.mark.asyncio
async def test_dry_run_is_read_only(monkeypatch, sample_intents):
    """The endpoint MUST NOT call `update_one`, `insert_one`,
    `replace_one`, or any other write on the SHARED_INTENTS
    collection. The router is dormant — dry-run can never accidentally
    mark a real intent."""
    from routes import admin_hot_brain_router as mod

    writes_observed: list[str] = []

    class _Cursor:
        def __init__(self, rows): self._rows = rows
        def sort(self, *a, **k): return self
        async def to_list(self, length): return self._rows[:length]

    class _ReadOnlyColl:
        def find(self, *a, **k):
            return _Cursor(sample_intents[:4])  # exclude HOLD
        def __getattr__(self, name):
            writes_observed.append(name)
            raise AssertionError(
                f"dry-run endpoint must be READ-ONLY — touched {name!r}"
            )

    class _DB:
        def __getitem__(self, name): return _ReadOnlyColl()

    monkeypatch.setattr(mod, "db", _DB())

    async def fake_perf(brain, lane, symbol, lookback=20):
        return _fake_perf("NEUTRAL")

    monkeypatch.setattr(mod, "get_recent_brain_performance", fake_perf)

    await mod.hot_brain_router_dry_run(days=1, _user={"sub": "test"})
    assert writes_observed == [], (
        f"dry-run endpoint must be read-only; observed writes: "
        f"{writes_observed!r}"
    )
