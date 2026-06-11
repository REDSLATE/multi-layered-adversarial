"""Shadow-outcome engine regression tests.

Pins the contract from the 2026-02-19 evening operator directive:
'Can we have it change the number without real cash being involved?'
"""
from __future__ import annotations

import pytest

from shared.doctrine.shadow_outcome import (
    _is_real_ticker,
    _label_for_pnl,
    _entry_price_from_snapshot,
)


@pytest.mark.parametrize(
    "ticker, expected",
    [
        ("AAPL", True),
        ("PLTR", True),
        ("F", True),                  # 1-char ticker (Ford)
        ("BRK.B", True),
        ("BRK-B", True),              # Berkshire-style with hyphen
        ("TRIPWIRE-07B11BF0", False), # synthetic system marker
        ("TRIPWIRE_GATE_CHAIN", False),
        ("aapl", True),               # _is_real_ticker uppercases first
        ("", False),
        ("TOOMANYCHARS", False),      # > 5 base chars
    ],
)
def test_is_real_ticker(ticker, expected):
    assert _is_real_ticker(ticker) is expected


@pytest.mark.parametrize(
    "pnl_pct, side, expected",
    [
        (0.025, "BUY", "win"),
        (-0.025, "BUY", "loss"),
        (0.0005, "BUY", "scratch"),  # under 0.1% threshold
        (-0.0005, "BUY", "scratch"),
        (0.05, "SELL", "win"),       # SELL sign already flipped upstream
    ],
)
def test_label_for_pnl(pnl_pct, side, expected):
    assert _label_for_pnl(pnl_pct, side) == expected


def test_entry_price_from_snapshot_prefers_last_price():
    assert _entry_price_from_snapshot({"last_price": 100.5}) == 100.5


def test_entry_price_from_snapshot_falls_through_to_other_keys():
    assert _entry_price_from_snapshot({"mark": 50.25}) == 50.25
    assert _entry_price_from_snapshot({"mid": 12.0}) == 12.0
    assert _entry_price_from_snapshot({"close": 88.88}) == 88.88


def test_entry_price_from_snapshot_returns_none_when_unavailable():
    assert _entry_price_from_snapshot({}) is None
    assert _entry_price_from_snapshot(None) is None
    # Zero/negative are treated as missing (defensive against bad data).
    assert _entry_price_from_snapshot({"last_price": 0}) is None
    assert _entry_price_from_snapshot({"last_price": -10}) is None
