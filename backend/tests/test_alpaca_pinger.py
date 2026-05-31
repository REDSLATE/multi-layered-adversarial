"""Tests for the Alpaca auto-pinger (2026-05-30).

Symmetric pinger to Kraken's auto-poller. Same doctrine: MC owns the
broker credentials, so MC owns the liveness stamp. Operator's runtime
tile should never go stale because nobody-clicked-/test in 17 hours.

These tests are pure-unit — they stub `get_alpaca_adapter` so they
don't hit Alpaca's live API. The lifecycle integration is exercised by
the existing server boot tests.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from db import db
from namespaces import ALPACA_CREDENTIALS
from shared.broker import alpaca_routes


@pytest.fixture
async def seeded_creds():
    """Insert a singleton Alpaca creds doc so the pinger has something
    to update. Cleanup after."""
    await db[ALPACA_CREDENTIALS].replace_one(
        {"_id": "singleton"},
        {
            "_id": "singleton",
            "api_key_enc": "x",  # never decrypted in these tests (adapter is mocked)
            "secret_key_enc": "x",
            "api_key_preview": "AKEY****",
            "secret_key_preview": "SKEY****",
            "execution_enabled": True,
            "connected_at": "2026-05-29T00:00:00+00:00",
            "last_ping_at": "2026-05-29T00:00:00+00:00",  # 24h+ stale
            "last_ping_ok": True,
        },
        upsert=True,
    )
    yield
    # Don't delete — the row may pre-exist on the dev DB. Reset to
    # a known stale state instead.
    await db[ALPACA_CREDENTIALS].update_one(
        {"_id": "singleton"},
        {"$set": {"last_ping_at": "2026-05-29T00:00:00+00:00"}},
    )


@pytest.mark.asyncio
async def test_pinger_tick_refreshes_last_ping_at_on_success(seeded_creds):
    """A successful tick MUST refresh `last_ping_at`, `last_ping_ok=True`,
    and capture the equity snapshot — same fields the manual /test
    endpoint refreshes. Otherwise the operator tile stays stale."""
    fake_adapter = AsyncMock()
    fake_adapter.ping = AsyncMock(return_value={
        "equity": 100000.0, "account_number": "PA123",
    })
    with patch(
        "shared.broker.alpaca_routes.get_alpaca_adapter",
        new=AsyncMock(return_value=fake_adapter),
    ):
        await alpaca_routes._pinger_tick()

    doc = await db[ALPACA_CREDENTIALS].find_one({"_id": "singleton"}, {"_id": 0})
    # Stamp moved off the 2026-05-29 stale anchor.
    assert doc["last_ping_at"] != "2026-05-29T00:00:00+00:00"
    assert doc["last_ping_ok"] is True
    assert doc["last_equity_snapshot"] == 100000.0
    assert doc["last_ping_error"] is None
    # Side-channel surface for the /pinger/status endpoint.
    assert alpaca_routes._PINGER_LAST_TICK["ok"] is True
    assert alpaca_routes._PINGER_LAST_TICK["equity"] == 100000.0


@pytest.mark.asyncio
async def test_pinger_tick_stamps_failure_without_crashing(seeded_creds):
    """Alpaca outage → `last_ping_ok=False` + `last_ping_error` set.
    The tick MUST NOT raise — otherwise the loop dies and we're back
    to the 17h staleness incident."""
    fake_adapter = AsyncMock()
    fake_adapter.ping = AsyncMock(side_effect=RuntimeError("alpaca 502"))
    with patch(
        "shared.broker.alpaca_routes.get_alpaca_adapter",
        new=AsyncMock(return_value=fake_adapter),
    ):
        await alpaca_routes._pinger_tick()  # must not raise

    doc = await db[ALPACA_CREDENTIALS].find_one({"_id": "singleton"}, {"_id": 0})
    assert doc["last_ping_ok"] is False
    assert "alpaca 502" in doc["last_ping_error"]
    assert alpaca_routes._PINGER_LAST_TICK["ok"] is False


@pytest.mark.asyncio
async def test_pinger_tick_noop_when_credentials_missing():
    """No credentials = no work, no exception. The stamp on the side
    surface reports `no_credentials` so operators can distinguish
    'broker down' from 'broker not connected'."""
    with patch(
        "shared.broker.alpaca_routes.get_alpaca_adapter",
        new=AsyncMock(return_value=None),
    ):
        await alpaca_routes._pinger_tick()
    assert alpaca_routes._PINGER_LAST_TICK["error"] == "no_credentials"


@pytest.mark.asyncio
async def test_start_pinger_is_idempotent():
    """Re-calling start while task is alive must be a no-op. Otherwise
    a server reload (or repeated lifespan event) would leak parallel
    pingers, doubling the Alpaca rate."""
    # First start
    alpaca_routes.start_pinger_if_needed()
    first_task = alpaca_routes._PINGER_TASK
    assert first_task is not None
    # Second start — should NOT replace.
    alpaca_routes.start_pinger_if_needed()
    assert alpaca_routes._PINGER_TASK is first_task
    # Clean up
    await alpaca_routes.stop_pinger()
    assert alpaca_routes._PINGER_TASK is None


@pytest.mark.asyncio
async def test_loop_swallows_tick_exceptions(seeded_creds):
    """The loop's outer try/except must contain ANY exception from
    `_pinger_tick` so the loop continues. Verified by patching
    `_pinger_tick` to raise on first call, succeed thereafter, and
    confirming the loop survives the first raise."""
    call_count = {"n": 0}

    async def flaky_tick():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("synthetic db failure")

    # Patch the loop's internal sleep so we can iterate fast without
    # the floor-of-30s guard slowing the test.
    real_sleep = asyncio.sleep

    async def fast_sleep(_secs):
        await real_sleep(0.01)

    with patch("shared.broker.alpaca_routes._pinger_tick", new=flaky_tick), \
         patch("shared.broker.alpaca_routes.asyncio.sleep", new=fast_sleep):
        alpaca_routes.start_pinger_if_needed()
        await real_sleep(0.1)
        await alpaca_routes.stop_pinger()

    assert call_count["n"] >= 2, "loop did not survive the first exception"
