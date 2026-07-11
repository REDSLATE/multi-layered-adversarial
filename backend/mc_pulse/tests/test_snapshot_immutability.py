"""MarketSnapshot immutability + factory validation."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from mc_pulse.snapshot import (
    MarketSnapshot,
    build_snapshot,
    freeze_indicators,
)


def _snap(**overrides):
    base = dict(
        symbol="NVDA",
        lane="equity",
        timestamp=datetime(2026, 7, 11, 14, 30, tzinfo=timezone.utc),
        price=Decimal("140.50"),
        indicators={"atr": 1.2, "rvol": 1.8},
    )
    base.update(overrides)
    return build_snapshot(**base)


# ── frozen + slots contract ──────────────────────────────────────

def test_snapshot_is_frozen():
    s = _snap()
    with pytest.raises(Exception):
        s.price = Decimal("999.00")  # type: ignore[misc]


def test_snapshot_has_no_dict():
    # slots=True means no __dict__, so brains cannot secretly
    # attach fields to the snapshot they were handed. This is the
    # per-brain isolation guarantee.
    s = _snap()
    assert not hasattr(s, "__dict__")


def test_indicators_are_read_only():
    s = _snap()
    with pytest.raises(TypeError):
        s.indicators["atr"] = 999.0  # type: ignore[index]


def test_indicators_get_still_works():
    # MappingProxyType must NOT break normal read access.
    s = _snap()
    assert s.indicators["atr"] == 1.2
    assert s.indicators.get("rvol") == 1.8
    assert s.indicators.get("missing", 0.0) == 0.0
    assert list(s.indicators.keys()) == ["atr", "rvol"]


def test_freeze_indicators_handles_none():
    m = freeze_indicators(None)
    assert dict(m) == {}
    with pytest.raises(TypeError):
        m["x"] = 1.0  # type: ignore[index]


def test_snapshot_id_is_unique_per_snapshot():
    a = _snap()
    b = _snap()
    assert a.snapshot_id != b.snapshot_id
    assert len(a.snapshot_id) == 16


# ── factory validation ──────────────────────────────────────────

def test_build_snapshot_uppercases_symbol():
    s = _snap(symbol="nvda")
    assert s.symbol == "NVDA"


def test_build_snapshot_lowercases_lane():
    s = _snap(lane="EQUITY")
    assert s.lane == "equity"


def test_build_snapshot_rejects_unknown_lane():
    with pytest.raises(ValueError):
        _snap(lane="options")


def test_build_snapshot_rejects_empty_symbol():
    with pytest.raises(ValueError):
        _snap(symbol="")


def test_build_snapshot_forces_utc_on_naive_timestamp():
    # Same 3-clock anti-regression as seat_key: naive dt → UTC,
    # never local time.
    naive = datetime(2026, 7, 11, 14, 30, 0)
    s = _snap(timestamp=naive)
    assert s.timestamp.tzinfo is timezone.utc


def test_build_snapshot_converts_non_utc_tz_to_utc():
    from datetime import timedelta
    et = timezone(timedelta(hours=-4))
    dt_et = datetime(2026, 7, 11, 10, 30, 0, tzinfo=et)
    s = _snap(timestamp=dt_et)
    assert s.timestamp.utcoffset().total_seconds() == 0
    assert s.timestamp.hour == 14


def test_build_snapshot_coerces_price_to_decimal():
    s = _snap(price=140.50)
    assert isinstance(s.price, Decimal)


def test_build_snapshot_default_market_state():
    s = _snap()
    assert s.market_state == "unknown"


def test_two_brains_cannot_share_indicator_mutation():
    # Simulates the exact contamination scenario: build the
    # snapshot, hand to brain A which mutates locally, then hand
    # to brain B. B must still see the ORIGINAL indicators.
    s = _snap()
    # Brain A pulls indicators out — read-only proxy.
    a_view = s.indicators
    with pytest.raises(TypeError):
        a_view["injected"] = 99.9  # type: ignore[index]
    # Brain B reads the same proxy — still clean.
    assert "injected" not in s.indicators
