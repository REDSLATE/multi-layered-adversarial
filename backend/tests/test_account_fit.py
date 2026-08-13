from shared.account_context import AccountSnapshot
from shared.account_fit import evaluate_account_fit


def snap(**kwargs):
    base = dict(
        lane="equity",
        broker="webull",
        captured_at_ms=1,
        equity=1000.0,
        cash=300.0,
        buying_power=300.0,
        positions=(),
        open_orders=(),
    )
    base.update(kwargs)
    return AccountSnapshot(**base)


def test_pass_clean_buy():
    fit = evaluate_account_fit(
        snapshot=snap(),
        symbol="AAPL",
        action="BUY",
        requested_notional=50,
    )
    assert fit.verdict == "PASS"


def test_duplicate_open_order_blocks():
    s = snap(open_orders=(
        {"symbol": "AAPL", "side": "BUY", "status": "open", "qty": 0, "notional": 25},
    ))
    fit = evaluate_account_fit(
        snapshot=s,
        symbol="AAPL",
        action="BUY",
        requested_notional=25,
    )
    assert fit.verdict == "BLOCK"
    assert "DUPLICATE_OPEN_ORDER" in fit.reasons


def test_exit_not_blocked_by_zero_buying_power():
    s = snap(
        buying_power=0,
        positions=(
            {"symbol": "AAPL", "side": "long", "market_value": 100},
        ),
    )
    fit = evaluate_account_fit(
        snapshot=s,
        symbol="AAPL",
        action="SELL",
        requested_notional=100,
    )
    assert fit.verdict == "PASS"


def test_reduce_to_buying_power():
    fit = evaluate_account_fit(
        snapshot=snap(),  # bp=300, equity=1000 → reserve 50, spendable 250
        symbol="AAPL",
        action="BUY",
        requested_notional=500,
    )
    assert fit.verdict == "REDUCE"
    assert "REDUCE_TO_BUYING_POWER" in fit.reasons
    assert 0 < fit.size_multiplier < 1


def test_single_name_cap_blocks_when_full():
    s = snap(positions=(
        {"symbol": "AAPL", "side": "long", "market_value": 260.0},
    ))
    fit = evaluate_account_fit(
        snapshot=s,  # cap = 250 (< existing 260) → no room
        symbol="AAPL",
        action="BUY",
        requested_notional=50,
    )
    assert fit.verdict == "BLOCK"
    assert "SINGLE_NAME_CAP" in fit.reasons


def test_sell_without_position_blocks():
    fit = evaluate_account_fit(
        snapshot=snap(),
        symbol="AAPL",
        action="SELL",
        requested_notional=50,
    )
    assert fit.verdict == "BLOCK"
    assert "NO_POSITION_TO_SELL" in fit.reasons
