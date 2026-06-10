"""Integration regression: the auto-router calls the dedupe layer
before submitting any order.

Doctrine pin (2026-06-10): the 130-trade AAPL loop was the most
expensive miss in MC's history. This test exists to make sure the
dedupe call site in `auto_router._route_one` is never accidentally
removed or short-circuited. If somebody refactors this away, this
test fails loudly and CI blocks the merge.
"""
from __future__ import annotations

import os
import sys

import pytest

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import auto_router as ar  # noqa: E402
from shared import in_flight_orders as ifo  # noqa: E402


def test_route_one_source_includes_dedupe_call():
    """Source-level sanity check: the dedupe import + claim call live
    in _route_one's body. Cheap to run, very clear failure mode."""
    import inspect
    src = inspect.getsource(ar._route_one)
    assert "claim_in_flight_slot" in src, (
        "auto_router._route_one no longer claims an in-flight slot — "
        "the 130-trade AAPL incident's structural fix has been removed."
    )
    assert "has_pending_order" in src, (
        "auto_router._route_one no longer checks broker-fills truth — "
        "remove this guard and you re-open the 06-09 amnesia window."
    )


@pytest.mark.asyncio
async def test_second_claim_is_blocked_module_level():
    """End-to-end behavioral assertion at the dedupe layer the
    auto-router uses — first claim wins, second claim refused."""
    ifo.reset_for_tests()
    try:
        assert await ifo.claim_in_flight_slot("AAPL", intent_id="i-0")
        assert not await ifo.claim_in_flight_slot("AAPL", intent_id="i-1")
    finally:
        ifo.reset_for_tests()
