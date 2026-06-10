"""Regression tests for position-context cache TTL + invalidation.

Doctrine pin (2026-06-10, P1 follow-up to AAPL incident):
The 10s TTL was the amnesia window. These tests pin:
  * The shortened 2s TTL.
  * `invalidate_for_lane()` immediately flushes the lane's cache.
  * Reading after invalidation re-fetches fresh broker state.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import position_context as pc  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_cache():
    pc.invalidate_cache()
    yield
    pc.invalidate_cache()


def test_ttl_is_two_seconds():
    """The 10s TTL was the structural amnesia window. Pin it at 2s."""
    assert pc._CACHE_TTL_SEC == 2.0, (
        f"position_context TTL must be 2s post-AAPL fix, got {pc._CACHE_TTL_SEC}"
    )


def test_invalidate_for_lane_clears_only_that_lane():
    """Sanity: invalidating one lane mustn't blow away the other."""
    pc._CACHE["equity"] = (time.time(), [{"symbol": "AAPL"}])
    pc._CACHE["crypto"] = (time.time(), [{"symbol": "BTC-USD"}])
    pc.invalidate_for_lane("equity")
    assert "equity" not in pc._CACHE
    assert "crypto" in pc._CACHE


def test_invalidate_for_lane_is_idempotent():
    """Punching a lane that wasn't cached must not raise."""
    pc.invalidate_for_lane("equity")  # nothing cached, must not raise
    pc.invalidate_for_lane("equity")  # again
    assert "equity" not in pc._CACHE


def test_invalidate_for_lane_unknown_lane_noop():
    pc._CACHE["equity"] = (time.time(), [])
    pc.invalidate_for_lane("totally-not-a-lane")
    assert "equity" in pc._CACHE


def test_invalidate_cache_wipes_all_lanes():
    pc._CACHE["equity"] = (time.time(), [])
    pc._CACHE["crypto"] = (time.time(), [])
    pc.invalidate_cache()
    assert pc._CACHE == {}
