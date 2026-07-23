"""Expectancy model tests — fee/spread cost math + aggregation."""
from __future__ import annotations

import pytest

from shared.expectancy import DEFAULTS, row_costs, _bucket, _fold, _finalize


CFG = {
    "crypto": {"taker_fee_pct": 0.40, "spread_bps": 20.0},
    "equity": {"taker_fee_pct": 0.0, "spread_bps": 5.0},
}


def test_row_costs_crypto_round_trip():
    row = {
        "lane": "crypto", "entry_price": 100.0, "exit_price": 102.0,
        "qty": 1.0, "realized_pnl_usd": 2.0,
    }
    c = row_costs(row, CFG)
    # turnover = 202; fee = 0.4% * 202 = 0.808; spread = 10bps/2side... 20bps/2 * 202 = 0.202
    assert c["gross"] == 2.0
    assert c["fee"] == pytest.approx(0.808)
    assert c["spread"] == pytest.approx(0.202)
    assert c["net"] == pytest.approx(2.0 - 0.808 - 0.202)


def test_row_costs_equity_zero_fee():
    row = {
        "lane": "equity", "entry_price": 10.0, "exit_price": 10.5,
        "qty": 1.0, "realized_pnl_usd": 0.5,
    }
    c = row_costs(row, CFG)
    assert c["fee"] == 0.0
    assert c["spread"] == pytest.approx(5.0 / 10_000.0 / 2.0 * 20.5)


def test_row_costs_unpriced_row_returns_none():
    assert row_costs({"lane": "crypto", "realized_pnl_usd": None}, CFG) is None


def test_row_costs_missing_exit_falls_back_to_entry():
    row = {
        "lane": "crypto", "entry_price": 50.0, "exit_price": None,
        "qty": 2.0, "realized_pnl_usd": 1.0,
    }
    c = row_costs(row, CFG)
    assert c["fee"] == pytest.approx(0.004 * 200.0)


def test_fee_drag_flips_small_winner_to_net_loser():
    # The whole point of the panel: +$0.05 gross on a $10 round trip
    # at crypto costs is a net LOSS.
    row = {
        "lane": "crypto", "entry_price": 10.0, "exit_price": 10.05,
        "qty": 1.0, "realized_pnl_usd": 0.05,
    }
    c = row_costs(row, CFG)
    assert c["gross"] > 0
    assert c["net"] < 0


def test_bucket_aggregation_and_finalize():
    b = _bucket()
    _fold(b, {"gross": 2.0, "fee": 0.5, "spread": 0.1, "net": 1.4})
    _fold(b, {"gross": -1.0, "fee": 0.4, "spread": 0.1, "net": -1.5})
    out = _finalize(b)
    assert out["trades"] == 2
    assert out["wins"] == 1 and out["losses"] == 1
    assert out["net_pnl_usd"] == pytest.approx(-0.1)
    assert out["win_rate_pct"] == 50.0
    assert out["expectancy_usd"] == pytest.approx(-0.05)
    assert out["avg_win_usd"] == pytest.approx(1.4)
    assert out["avg_loss_usd"] == pytest.approx(-1.5)
    assert out["profit_factor"] == pytest.approx(1.4 / 1.5, abs=1e-3)


def test_defaults_shape():
    assert set(DEFAULTS) == {"equity", "crypto"}
    for lane in DEFAULTS.values():
        assert set(lane) == {"taker_fee_pct", "spread_bps"}
