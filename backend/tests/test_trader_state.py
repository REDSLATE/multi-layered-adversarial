"""Tests for /app/trader/state.py — in-memory cache resilience.

Proves that when Mongo is unreachable, the trader still resolves
seat assignments and flags from either the SQLite last-good
snapshot or the hard-coded DEFAULT_SEATS. This is the guarantee
that keeps the trader trading during an Atlas outage.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

sys.path.insert(0, "/app")

from trader import state, store  # noqa: E402


@pytest.fixture()
def fresh(tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    store.init(str(tmp_path / "s.sqlite"), str(tmp_path / "j"))
    # Wipe the module-level cache between tests.
    state._seats.clear()
    state._governor_mult.clear()
    state._lane_enabled.clear()
    state._lane_enabled.update({
        "equity": state.DEFAULT_LANE_ENABLED,
        "crypto": state.DEFAULT_LANE_ENABLED,
    })
    # master armed default is False (safe)
    import trader.state as _s
    _s._master_armed = state.DEFAULT_MASTER_ARMED
    yield
    store.close()
    loop.close()
    asyncio.set_event_loop(None)


def test_cold_boot_returns_defaults(fresh):
    """With nothing hydrated, seat lookups return the canonical
    angel-brain pairings so a fresh deploy is never seat-vacant."""
    seats = state.get_lane_seats("equity")
    assert seats["executor"] == "gto"
    assert seats["strategist"] == "camino"
    seats_c = state.get_lane_seats("crypto")
    assert seats_c["executor"] == "gto"
    assert seats_c["strategist"] == "hellcat"


def test_defaults_apply_to_unknown_lane(fresh):
    assert state.get_lane_seats("nonexistent") == {}


def test_master_switch_default_is_disarmed(fresh):
    """Safety default: no Mongo doc → not armed. Operator arms
    explicitly via MC."""
    assert state.master_switch_armed() is False


def test_lane_enabled_default_true(fresh):
    assert state.lane_enabled("equity") is True
    assert state.lane_enabled("crypto") is True


def test_governor_multiplier_default_one(fresh):
    assert state.governor_multiplier("equity") == 1.0
    assert state.governor_multiplier("crypto") == 1.0


def test_hydrate_from_sqlite_restores_last_good(fresh):
    """Simulate a prior successful Mongo pull that persisted to
    SQLite. Blow away the in-memory cache. Hydrate. Verify the
    persisted values are back."""
    store.upsert_seat_cache("equity:executor", "equity", "executor",
                            "barracuda", None)
    store.upsert_seat_cache("equity:governor", "equity", "governor",
                            "gto", 1.75)
    store.upsert_flag_cache("master_armed", True)
    store.upsert_flag_cache("lane_enabled", {"equity": True, "crypto": False})

    state._seats.clear()
    state._governor_mult.clear()
    state._lane_enabled.clear()

    state.hydrate_from_sqlite()

    assert state.get_lane_seats("equity")["executor"] == "barracuda"
    assert state.governor_multiplier("equity") == 1.75
    assert state.master_switch_armed() is True
    assert state.lane_enabled("crypto") is False


def test_snapshot_shape(fresh):
    snap = state.snapshot()
    assert "seats" in snap
    assert "master_armed" in snap
    assert "lane_enabled" in snap
    assert "last_refresh_ok_ts" in snap


def test_manual_refresh_no_worker_returns_false(fresh):
    """When the refresh loop isn't running, request_manual_refresh
    returns False so the API can tell the operator why nothing
    happened."""
    # No refresh_loop task has been started
    state._manual_refresh_event = None
    assert state.request_manual_refresh() is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
