from shared.risk.take_profit_guard import take_profit_guard


def test_long_hits_take_profit():
    v = take_profit_guard(
        side="LONG",
        entry_price=100,
        current_price=103,
        take_profit_pct=3.0,
    )
    assert v.action == "CLOSE"
    assert v.pnl_pct == 3.0


def test_short_hits_take_profit():
    v = take_profit_guard(
        side="SHORT",
        entry_price=100,
        current_price=97,
        take_profit_pct=3.0,
    )
    assert v.action == "CLOSE"
    assert v.pnl_pct == 3.0


def test_partial_take_profit():
    v = take_profit_guard(
        side="LONG",
        entry_price=100,
        current_price=102,
        take_profit_pct=3.0,
        partial_take_pct=2.0,
    )
    assert v.action == "REDUCE"
    assert v.close_fraction == 0.5


def test_no_take_profit_yet():
    v = take_profit_guard(
        side="LONG",
        entry_price=100,
        current_price=101,
        take_profit_pct=3.0,
    )
    assert v.action == "HOLD"
