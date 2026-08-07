"""Unit tests — RISE outcome engine (triple barrier + attribution)."""
from datetime import datetime, timedelta, timezone

from shared.outcome_engine.engine import (
    Attribution, BarrierOutcome, ExecutionAttributionEngine,
    ExecutionSnapshot, PricePoint, Side, SignalSnapshot, TripleBarrierEngine,
)

T0 = datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc)


def _signal(**kw):
    base = dict(
        signal_id="s1", symbol="TEST", lane="equity", brain="gto",
        side=Side.BUY, signal_time=T0, signal_price=100.0, confidence=0.7,
        profit_target_pct=0.06, stop_loss_pct=0.03, max_holding_seconds=3600,
    )
    base.update(kw)
    return SignalSnapshot(**base)


def _points(seq):
    return [PricePoint(timestamp=T0 + timedelta(minutes=i + 1), price=p)
            for i, p in enumerate(seq)]


def test_profit_barrier_hit():
    r = TripleBarrierEngine.evaluate(_signal(), _points([101, 103, 106.5]))
    assert r.outcome == BarrierOutcome.PROFIT
    assert r.return_pct > 0.06 - 1e-9
    assert r.max_favorable_excursion_pct >= r.return_pct - 1e-9


def test_stop_barrier_hit():
    r = TripleBarrierEngine.evaluate(_signal(), _points([99, 98, 96.9]))
    assert r.outcome == BarrierOutcome.STOP
    assert r.return_pct < 0


def test_stop_checked_before_profit_same_point_conservative():
    # a bar whose low breaches stop AND high breaches profit → STOP wins
    pts = [PricePoint(timestamp=T0 + timedelta(minutes=1), price=96.0),
           PricePoint(timestamp=T0 + timedelta(minutes=1), price=107.0)]
    r = TripleBarrierEngine.evaluate(_signal(), pts)
    assert r.outcome == BarrierOutcome.STOP


def test_time_expiry():
    r = TripleBarrierEngine.evaluate(_signal(), _points([100.5, 100.2, 100.8]))
    assert r.outcome == BarrierOutcome.TIME


def test_no_data_unknown():
    r = TripleBarrierEngine.evaluate(_signal(), [])
    assert r.outcome == BarrierOutcome.UNKNOWN


def test_sell_side_barriers():
    sig = _signal(side=Side.SELL)
    r = TripleBarrierEngine.evaluate(sig, _points([98, 95, 93.9]))
    assert r.outcome == BarrierOutcome.PROFIT
    assert r.return_pct > 0


def test_points_outside_window_ignored():
    late = [PricePoint(timestamp=T0 + timedelta(hours=2), price=50.0)]
    r = TripleBarrierEngine.evaluate(_signal(), late)
    assert r.outcome == BarrierOutcome.UNKNOWN


ATTR = ExecutionAttributionEngine()


def _theoretical(seq, sig=None):
    return TripleBarrierEngine.evaluate(sig or _signal(), _points(seq))


def test_bad_signal():
    th = _theoretical([98, 96.5])
    ex = ExecutionSnapshot(signal_id="s1", executed=False)
    assert ATTR.classify(_signal(), ex, th) == Attribution.BAD_SIGNAL


def test_good_signal_gate_rejected():
    th = _theoretical([103, 106.5])
    ex = ExecutionSnapshot(signal_id="s1", executed=False,
                           gate_rejection_reason="exit_only_mode")
    assert ATTR.classify(_signal(), ex, th) == Attribution.GOOD_SIGNAL_GATE_REJECTED


def test_good_signal_not_executed():
    th = _theoretical([103, 106.5])
    ex = ExecutionSnapshot(signal_id="s1", executed=False)
    assert ATTR.classify(_signal(), ex, th) == Attribution.GOOD_SIGNAL_NOT_EXECUTED


def test_good_signal_late_entry():
    th = _theoretical([103, 106.5])
    ex = ExecutionSnapshot(
        signal_id="s1", executed=True,
        entry_time=T0 + timedelta(minutes=5), entry_price=102.0,
        exit_time=T0 + timedelta(minutes=30), exit_price=103.0)
    assert ATTR.classify(_signal(), ex, th) == Attribution.GOOD_SIGNAL_LATE_ENTRY


def test_good_entry_bad_exit():
    th = _theoretical([103, 106.5])
    ex = ExecutionSnapshot(
        signal_id="s1", executed=True,
        entry_time=T0 + timedelta(seconds=10), entry_price=100.2,
        exit_time=T0 + timedelta(minutes=30), exit_price=99.0)
    assert ATTR.classify(_signal(), ex, th) == Attribution.GOOD_ENTRY_BAD_EXIT


def test_good_complete_trade():
    th = _theoretical([103, 106.5])
    ex = ExecutionSnapshot(
        signal_id="s1", executed=True,
        entry_time=T0 + timedelta(seconds=5), entry_price=100.1,
        exit_time=T0 + timedelta(minutes=20), exit_price=106.0)
    assert ATTR.classify(_signal(), ex, th) == Attribution.GOOD_COMPLETE_TRADE


def test_execution_slippage_partial_capture():
    th = _theoretical([103, 106.5])
    ex = ExecutionSnapshot(
        signal_id="s1", executed=True,
        entry_time=T0 + timedelta(seconds=5), entry_price=100.5,
        exit_time=T0 + timedelta(minutes=20), exit_price=102.0)
    assert ATTR.classify(_signal(), ex, th) == Attribution.EXECUTION_SLIPPAGE


def test_no_meaningful_edge():
    th = _theoretical([100.05, 100.02, 100.01])
    ex = ExecutionSnapshot(signal_id="s1", executed=False)
    assert ATTR.classify(_signal(), ex, th) == Attribution.NO_MEANINGFUL_EDGE


def test_insufficient_data():
    th = _theoretical([])
    ex = ExecutionSnapshot(signal_id="s1", executed=False)
    assert ATTR.classify(_signal(), ex, th) == Attribution.INSUFFICIENT_DATA


def test_entry_slippage_sign_buy():
    ex = ExecutionSnapshot(signal_id="s1", executed=True,
                           entry_time=T0, entry_price=101.0)
    slip = ATTR.calculate_entry_slippage(_signal(), ex)
    assert slip > 0  # paid more than signal price = positive slippage


def test_store_roundtrip(tmp_path, monkeypatch):
    from shared.outcome_engine import store as st
    monkeypatch.setattr(st, "_DB_PATH", str(tmp_path / "t.sqlite"))
    monkeypatch.setattr(st, "_conn", None)
    rec = {c: None for c in st._COLS}
    rec.update(outcome_id="o1", signal_id="sig1", symbol="TEST",
               lane="equity", brain="gto", side="BUY",
               signal_time=T0.isoformat(), signal_price=100.0,
               confidence=0.7, theoretical_outcome="PROFIT",
               theoretical_return_pct=0.06, executed=True,
               attribution="GOOD_COMPLETE_TRADE", metadata_json="{}",
               created_at=T0.isoformat())
    st.save(rec)
    assert st.has_signal("sig1")
    assert not st.has_signal("sig2")
    rows = st.recent(10)
    assert rows and rows[0]["signal_id"] == "sig1"
    roll = st.rollup()
    assert roll["total"] == 1
    assert roll["by_attribution"][0]["attribution"] == "GOOD_COMPLETE_TRADE"
    assert roll["by_brain"][0]["brain"] == "gto"
