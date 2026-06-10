"""Smoke tests for the AAPL replay script's pure-function helpers.

These tests do NOT touch Mongo or Polygon — they only exercise the
classification, signed-qty walk, and P&L delta helpers in
`backend/scripts/replay_aapl_2026_06_09.py`. The integration path
(load_intents → fetch_minute_bars → run_replay) is exercised by
running the script itself; this file pins the math.
"""
import sys

sys.path.insert(0, "/app/backend")
sys.path.insert(0, "/app/backend/scripts")

# Import the script as a module. We deliberately import the
# free functions, not run_replay, because the latter touches Mongo.
from replay_aapl_2026_06_09 import (  # noqa: E402
    _actual_pnl_delta,
    _apply_to_signed_qty,
    _classify,
    _edge_pnl_delta,
    _mark_for_ts,
)


# ── _classify routes through the new position-state model ────────


def test_classify_buy_against_short_is_reduce_partial_cover():
    """The AAPL fix in one assertion: BUY emitted while short = the
    primitive is REDUCE, the evolution is PARTIAL_COVER. Not OPEN."""
    primitive, target_side, evo, risk = _classify(
        action="BUY",
        signed_qty_before=-100.0,
        order_qty=10.0,
        confidence=0.5,
        market_regime="calm",
    )
    assert primitive == "REDUCE"
    assert evo == "PARTIAL_COVER"
    # RISK_OFF would require a stressed regime — this is NEUTRAL.
    assert risk == "NEUTRAL"


def test_classify_full_cover_in_volatile_regime_lifts_to_risk_off():
    primitive, target_side, evo, risk = _classify(
        action="BUY",
        signed_qty_before=-10.0,
        order_qty=10.0,    # exact close
        confidence=0.90,   # ≥ FULL_COVER_CONFIDENCE_FLOOR
        market_regime="volatile",
    )
    assert primitive == "CLOSE"
    assert evo == "FULL_COVER"
    assert risk == "RISK_OFF"


def test_classify_high_conviction_add_long_in_calm_is_scale_in_risk_on():
    primitive, target_side, evo, risk = _classify(
        action="BUY",
        signed_qty_before=50.0,
        order_qty=10.0,
        confidence=0.75,
        market_regime="calm",
    )
    assert primitive == "ADD"
    assert evo == "SCALE_IN"
    assert risk == "RISK_ON"


def test_classify_hold_passes_through():
    primitive, target_side, evo, risk = _classify(
        action="HOLD",
        signed_qty_before=-100.0,
        order_qty=0.0,
        confidence=0.5,
        market_regime="volatile",
    )
    assert primitive == "HOLD"
    assert evo == "HOLD"
    assert risk == "NEUTRAL"


# ── signed_qty walk ───────────────────────────────────────────────


def test_signed_qty_walk_buy_subtracts_short():
    """BUY against a SHORT moves signed_qty toward 0 from below."""
    assert _apply_to_signed_qty(-100.0, "BUY", 30.0) == -70.0


def test_signed_qty_walk_buy_from_flat_goes_long():
    assert _apply_to_signed_qty(0.0, "BUY", 50.0) == 50.0


def test_signed_qty_walk_sell_from_long():
    assert _apply_to_signed_qty(100.0, "SELL", 40.0) == 60.0


def test_signed_qty_walk_hold_is_identity():
    assert _apply_to_signed_qty(-100.0, "HOLD", 5.0) == -100.0


# ── P&L deltas: the actual-vs-edge asymmetry on a short cover ─────


def test_actual_pnl_delta_buy_is_negative_cash_flow():
    """BUY at mark=200, qty=1 → broker debits cash → -200 delta."""
    assert _actual_pnl_delta("BUY", -100.0, 1.0, 200.0) == -200.0


def test_edge_pnl_delta_buy_on_short_is_positive_cash_in():
    """Same trade reinterpreted: brain meant COVER → cash IN at mark
    → +200 delta. This is the AAPL edge: when the brain emits BUY
    against an open short, the CORRECT semantics flip the sign vs
    the broker's naive read."""
    # Primitive REDUCE on a SHORT (signed_qty_before < 0) = COVER.
    assert _edge_pnl_delta("REDUCE", -100.0, 1.0, 200.0) == +200.0


def test_edge_pnl_delta_open_long_matches_actual():
    """When the brain genuinely means OPEN_LONG, edge tracks actual
    (both are negative cash flow)."""
    assert _edge_pnl_delta("OPEN", 0.0, 1.0, 200.0) == -200.0
    assert _actual_pnl_delta("BUY", 0.0, 1.0, 200.0) == -200.0


def test_edge_pnl_delta_zero_without_mark():
    """No mark = no P&L attribution (the semantic timeline still
    works; only the dollar columns are zeroed)."""
    assert _edge_pnl_delta("REDUCE", -100.0, 1.0, None) == 0.0


# ── _mark_for_ts ──────────────────────────────────────────────────


def test_mark_for_ts_picks_last_bar_before_intent():
    # Two bars at 14:30 and 14:35. Intent at 14:33 → picks 14:30.
    bars = {
        1717943400000: 195.50,  # 2024-06-09T14:30:00Z (placeholder ms)
        1717943700000: 196.10,  # +5 min
    }
    # Build ts strings that align with the bar epochs.
    from datetime import datetime as _dt, timezone as _tz
    ts_in_between = _dt.fromtimestamp(
        (1717943400000 + 90_000) / 1000, tz=_tz.utc,
    ).isoformat()
    assert _mark_for_ts(bars, ts_in_between) == 195.50


def test_mark_for_ts_empty_returns_none():
    assert _mark_for_ts({}, "2026-06-09T14:30:00Z") is None
