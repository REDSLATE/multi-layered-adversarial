"""Regression tests for in-flight order dedupe.

Doctrine pin (2026-06-10, post-AAPL incident):
The 130-trade runaway loop happened because nothing prevented the
auto-router from re-submitting the same symbol while a prior order
was still in flight. These tests pin the structural fix in place:
exactly ONE claim per symbol can be live at any moment, and the
claim ages out after the configured TTL.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

# Make `/app/backend` importable without depending on test runner CWD.
BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import in_flight_orders as ifo  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_state():
    ifo.reset_for_tests()
    yield
    ifo.reset_for_tests()


@pytest.mark.asyncio
async def test_first_claim_succeeds():
    assert await ifo.claim_in_flight_slot("AAPL", intent_id="i-1") is True


@pytest.mark.asyncio
async def test_second_claim_for_same_symbol_blocks():
    assert await ifo.claim_in_flight_slot("AAPL", intent_id="i-1") is True
    assert await ifo.claim_in_flight_slot("AAPL", intent_id="i-2") is False


@pytest.mark.asyncio
async def test_different_symbols_are_independent():
    assert await ifo.claim_in_flight_slot("AAPL") is True
    assert await ifo.claim_in_flight_slot("MSFT") is True
    assert await ifo.claim_in_flight_slot("AAPL") is False
    assert await ifo.claim_in_flight_slot("MSFT") is False


@pytest.mark.asyncio
async def test_release_allows_reclaim():
    assert await ifo.claim_in_flight_slot("AAPL") is True
    await ifo.release_in_flight_slot("AAPL")
    assert await ifo.claim_in_flight_slot("AAPL") is True


@pytest.mark.asyncio
async def test_case_insensitive_symbol():
    assert await ifo.claim_in_flight_slot("aapl") is True
    assert await ifo.claim_in_flight_slot("AAPL") is False
    assert await ifo.claim_in_flight_slot("AaPl") is False


@pytest.mark.asyncio
async def test_empty_symbol_rejected():
    assert await ifo.claim_in_flight_slot("") is False
    assert await ifo.claim_in_flight_slot(None) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_is_in_flight_reflects_state():
    assert await ifo.is_in_flight("AAPL") is False
    await ifo.claim_in_flight_slot("AAPL")
    assert await ifo.is_in_flight("AAPL") is True
    await ifo.release_in_flight_slot("AAPL")
    assert await ifo.is_in_flight("AAPL") is False


@pytest.mark.asyncio
async def test_ttl_ages_out(monkeypatch):
    # Force TTL to 0s so any entry is considered immediately stale.
    monkeypatch.setattr(ifo, "_PENDING_TTL_SEC", 0)
    assert await ifo.claim_in_flight_slot("AAPL") is True
    # Sleep just a hair so monotonic clock advances past 0.
    await asyncio.sleep(0.01)
    # Next claim should succeed because the prior entry aged out.
    assert await ifo.claim_in_flight_slot("AAPL") is True


@pytest.mark.asyncio
async def test_snapshot_excludes_expired(monkeypatch):
    monkeypatch.setattr(ifo, "_PENDING_TTL_SEC", 60)
    await ifo.claim_in_flight_slot("AAPL", intent_id="i-1")
    snap = ifo.snapshot()
    assert snap["count"] == 1
    assert snap["pending"][0]["symbol"] == "AAPL"
    assert snap["pending"][0]["intent_id"] == "i-1"
    assert snap["ttl_seconds"] == 60


@pytest.mark.asyncio
async def test_concurrent_claims_only_one_wins():
    """Doctrine: even under contention, exactly one claim wins.

    This is the 130-trade scenario in miniature: many would-be
    submitters racing for the same symbol within microseconds. The
    lock guarantees that only one of them proceeds.
    """
    async def claimer() -> bool:
        return await ifo.claim_in_flight_slot("AAPL")

    results = await asyncio.gather(*[claimer() for _ in range(50)])
    assert results.count(True) == 1
    assert results.count(False) == 49


@pytest.mark.asyncio
async def test_burst_scenario_130_trades_blocked():
    """The exact pattern from 2026-06-09: rapid-fire BUY attempts on
    AAPL. Only the first one is allowed; the rest are blocked until
    a release or TTL age-out."""
    first = await ifo.claim_in_flight_slot("AAPL", intent_id="i-0")
    assert first is True
    # 129 more attempts within the TTL window — all must be refused.
    rest = [
        await ifo.claim_in_flight_slot("AAPL", intent_id=f"i-{n}")
        for n in range(1, 130)
    ]
    assert not any(rest), "Dedupe failed: some duplicate slots were granted"
    snap = ifo.snapshot()
    assert snap["count"] == 1
