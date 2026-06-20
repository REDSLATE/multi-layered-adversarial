"""Research Layer unit tests.

Doctrine pinned by these tests:
  1. Strategy Lab can score → produces non-empty signals on synthetic
     trend/breakdown bars.
  2. Brains can opine → `attach_research_to_intent` writes into the
     `evidence.research_signals` slot and NOWHERE else.
  3. Seats can execute → there is NO submit / route / broker call
     reachable from `shared.research`. Surface check: no symbols
     named *submit*, *broker*, *route* are exported.
  4. RoadGuard can stop → research never sets `executed`, `gate_state`,
     or any pipeline-mutating key on the intent.
"""
from __future__ import annotations

import shared.research as research_pkg
from shared.research import (
    attach_research_to_intent,
    build_features,
    score_strategies,
)
from shared.research.backtest import backtest_strategy
from shared.research.strategy_lab import (
    crypto_breakdown,
    large_cap_momentum,
)


# ── Synthetic bar fixtures ───────────────────────────────────────────
def _bull_run(n: int = 80, start: float = 100.0) -> list[dict]:
    """Accelerating uptrend with mild pullbacks + late volume surge.
    Crafted to satisfy `large_cap_momentum`: VWAP < close, MACD line
    diverging above its signal, rvol > 1.5 on the latest bar.
    (Wilder RSI bands relax — the strategy still fires off the other
    three confirming legs.)
    """
    bars: list[dict] = []
    price = start
    for i in range(n):
        base = 0.2 + (i / n) * 0.6
        step = -base * 0.3 if i % 5 == 4 else base
        o = price
        c = price + step
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1  # noqa: E741
        v = 1_000 if i < n - 3 else 5_000
        bars.append({"ts": i, "o": o, "h": h, "l": l, "c": c, "v": v})
        price = c
    return bars


def _bear_breakdown(n: int = 80, start: float = 100.0) -> list[dict]:
    """Accelerating downtrend with mild rallies + late volume surge."""
    bars: list[dict] = []
    price = start
    for i in range(n):
        base = 0.2 + (i / n) * 0.6
        step = base * 0.3 if i % 5 == 4 else -base
        o = price
        c = price + step
        h = max(o, c) + 0.1
        l = min(o, c) - 0.1  # noqa: E741
        v = 1_000 if i < n - 3 else 4_000
        bars.append({"ts": i, "o": o, "h": h, "l": l, "c": c, "v": v})
        price = c
    return bars


def _flat(n: int = 80, price: float = 100.0) -> list[dict]:
    return [
        {"ts": i, "o": price, "h": price + 0.05, "l": price - 0.05,
         "c": price, "v": 1_000}
        for i in range(n)
    ]


# ── 1. Strategy Lab can score ────────────────────────────────────────
def test_large_cap_momentum_fires_buy_on_bull_run():
    bars = _bull_run()
    f = build_features("AAPL", "equity", bars)
    sig = large_cap_momentum(f)
    assert sig.direction == "BUY", sig
    assert sig.score >= 0.55
    assert "macd_bullish" in sig.reasons


def test_large_cap_momentum_holds_on_flat():
    f = build_features("AAPL", "equity", _flat())
    sig = large_cap_momentum(f)
    assert sig.direction == "HOLD"


def test_crypto_breakdown_fires_sell_on_breakdown():
    bars = _bear_breakdown()
    f = build_features("BTC/USD", "crypto", bars, spread_bps=15)
    sig = crypto_breakdown(f)
    assert sig.direction == "SELL", sig
    assert "macd_bearish" in sig.reasons


def test_crypto_breakdown_wide_spread_penalty_subtracts():
    """A wide spread should cost the signal points — high spread on
    an otherwise-good breakdown should sit at the threshold edge."""
    bars = _bear_breakdown()
    no_spread = crypto_breakdown(build_features("BTC/USD", "crypto", bars))
    wide = crypto_breakdown(
        build_features("BTC/USD", "crypto", bars, spread_bps=500.0)
    )
    assert wide.score < no_spread.score
    assert "wide_spread_penalty" in wide.reasons


def test_score_strategies_lane_dispatch():
    eq = score_strategies(build_features("AAPL", "equity", _bull_run()))
    cr = score_strategies(build_features("BTC/USD", "crypto", _bear_breakdown()))
    # One strategy per lane today — pin the count so a regression that
    # leaks cross-lane scoring (e.g. running crypto_breakdown on
    # equities) trips the test.
    assert len(eq) == 1
    assert len(cr) == 1
    assert eq[0].strategy_id == "large_cap_momentum_v1"
    assert cr[0].strategy_id == "crypto_breakdown_v1"


def test_unknown_lane_silently_abstains():
    f = build_features("FOO", "fx", _bull_run())
    assert score_strategies(f) == []


def test_cold_start_returns_hold_not_buy():
    """Fewer than 50 bars → MACD/SMA warmup not complete → must HOLD
    even if the few bars we have are bullish."""
    bars = _bull_run(n=10)
    f = build_features("AAPL", "equity", bars)
    sig = large_cap_momentum(f)
    assert sig.direction == "HOLD"


# ── 2. Brains can opine — bridge writes only to evidence ─────────────
def test_attach_research_writes_only_to_evidence():
    intent = {
        "brain": "hellcat",
        "symbol": "ETH/USD",
        "action": "SELL",
        "confidence": 0.69,
    }
    snapshot_keys_before = set(intent.keys())
    signals = score_strategies(
        build_features("ETH/USD", "crypto", _bear_breakdown())
    )
    out = attach_research_to_intent(intent, signals)
    assert out is intent  # in-place, returns same ref for chaining
    # No top-level key drift — only `evidence` was added.
    assert set(out.keys()) - snapshot_keys_before == {"evidence"}
    assert "research_signals" in out["evidence"]
    assert isinstance(out["evidence"]["research_signals"], list)
    # Re-run is idempotent (replaces, doesn't append).
    attach_research_to_intent(intent, signals)
    assert len(out["evidence"]["research_signals"]) == len(signals)


def test_attach_research_does_not_mutate_action_or_confidence():
    intent = {"action": "SELL", "confidence": 0.69, "executed": False}
    attach_research_to_intent(
        intent,
        score_strategies(build_features("ETH/USD", "crypto", _bear_breakdown())),
    )
    assert intent["action"] == "SELL"
    assert intent["confidence"] == 0.69
    assert intent["executed"] is False


# ── 3. Seats can execute — research surface MUST NOT expose any
#       broker / submit / route helper. ────────────────────────────────
def test_research_package_surface_is_read_only():
    forbidden_substrings = ("submit", "broker", "route", "execute", "place_order")
    exported = [n for n in dir(research_pkg) if not n.startswith("_")]
    for name in exported:
        lo = name.lower()
        for bad in forbidden_substrings:
            assert bad not in lo, (
                f"Research Layer must stay read-only — {name!r} "
                f"contains forbidden token {bad!r}"
            )


# ── 4. RoadGuard can stop — research never touches pipeline keys ─────
def test_attach_research_does_not_touch_pipeline_keys():
    intent = {"action": "BUY", "confidence": 0.8}
    attach_research_to_intent(
        intent,
        score_strategies(build_features("AAPL", "equity", _bull_run())),
    )
    for forbidden in ("gate_state", "dry_run_state", "executed",
                       "pipeline_receipt", "broker_route"):
        assert forbidden not in intent, (
            f"Research must not write {forbidden!r}"
        )


# ── 5. Backtest sanity ───────────────────────────────────────────────
def test_backtest_returns_summary_on_warm_window():
    bars = _bull_run(n=120)
    res = backtest_strategy(bars, large_cap_momentum, "AAPL", "equity")
    assert res["bars"] == 120
    assert res["signals_total"] >= 0
    # win_rate is None when there are zero non-HOLD steps; otherwise
    # a clean fraction in [0, 1].
    if res["win_rate"] is not None:
        assert 0.0 <= res["win_rate"] <= 1.0


def test_backtest_cold_window_returns_safe_zeros():
    res = backtest_strategy(_bull_run(n=20), large_cap_momentum, "AAPL", "equity")
    assert res["signals_total"] == 0
    assert res["win_rate"] is None
    assert "warmup_required" in res
