"""Regression: Webull SDK calls MUST be dispatched on a thread executor.

Operator incident (2026-02-19): the prod pod was crashing after ~15
minutes of auto-router ticks with Cloudflare 520s across every authed
endpoint. Root cause: every Webull SDK method
(`get_account_list`, `get_account_detail`, `place_order`, …) is a
synchronous, blocking HTTPS round-trip. Calling them directly from
`async def` methods starves the event loop for the duration of each
call. Under the auto-router's 5-intent / 30s tick load, this
compounded until external request handlers (auth, /api/admin/…) hit
the Cloudflare 30s gateway timeout and the pod was killed.

Fix: every SDK call now goes through `WebullAdapter._sdk_call`, which
wraps the call in `asyncio.get_running_loop().run_in_executor(...)`.
This test pins the contract by asserting that calling
`submit_market_order` against a stub SDK does NOT block the running
event loop — concurrent coroutines make progress while the stubbed
SDK call "blocks" on a synthetic sleep.
"""
import asyncio
import sys
import time

sys.path.insert(0, "/app/backend")

import pytest

from shared.broker.webull import WebullAdapter


class _StubResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _StubAccountV2:
    def get_account_list(self):
        # Simulate a slow HTTPS round-trip — this is what blocks the
        # event loop when called without run_in_executor.
        time.sleep(0.5)
        return _StubResponse({"code": "200", "data": [{"accountId": "ACC-1"}]})

    def get_account_detail(self, account_id):  # noqa: ARG002
        time.sleep(0.5)
        return _StubResponse({"data": {"cashBalance": 1000.0, "buyingPower": 1000.0}})


class _StubOrderV1:
    def place_order(self, payload):  # noqa: ARG002
        time.sleep(0.5)
        return _StubResponse({"data": {"orderId": "ORD-1", "status": "SUBMITTED"}})


class _StubTradeClient:
    account_v2 = _StubAccountV2()
    order = _StubOrderV1()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.setenv("WEBULL_ARMED", "true")
    monkeypatch.delenv("WEBULL_MIN_NOTIONAL_USD", raising=False)
    monkeypatch.delenv("WEBULL_MAX_NOTIONAL_USD", raising=False)
    yield


def _build_stub_adapter(monkeypatch):
    """WebullAdapter wired against the _StubTradeClient so the SDK
    surface is exercised without burning the operator's real API
    quota."""
    adapter = WebullAdapter(api_client=object(), account_id="ACC-1")
    monkeypatch.setattr(adapter, "_trade", lambda: _StubTradeClient())
    return adapter


@pytest.mark.asyncio
async def test_submit_market_order_does_not_block_event_loop(monkeypatch):
    """Submit + a parallel sleep-coroutine. If the SDK call blocked
    the loop, the parallel sleep would have to wait for the SDK
    sleep to complete. With `run_in_executor` they run in parallel.
    """
    adapter = _build_stub_adapter(monkeypatch)

    progress = {"ticks": 0}

    async def heartbeat():
        for _ in range(10):
            await asyncio.sleep(0.05)
            progress["ticks"] += 1

    t_start = asyncio.get_running_loop().time()
    submit_task = asyncio.create_task(
        adapter.submit_market_order(symbol="AAPL", notional=5.0, side="BUY"),
    )
    heart_task = asyncio.create_task(heartbeat())
    order, _ = await asyncio.gather(submit_task, heart_task)
    elapsed = asyncio.get_running_loop().time() - t_start

    # Heartbeat should have ticked through at least 8 cycles (10 ×
    # 0.05s = 0.5s) while the SDK was "sleeping" — proves the SDK
    # call did NOT block the event loop.
    assert progress["ticks"] >= 8, (
        f"event loop was blocked during SDK call — heartbeat only "
        f"reached {progress['ticks']} ticks"
    )
    # Submit must have returned a real order envelope.
    assert order["order_id"] == "ORD-1"
    # End-to-end should be ~max(0.5s, 0.5s) = ~0.5s, NOT 1.0s
    # (which would mean it ran serially).
    assert elapsed < 0.9, (
        f"submit took {elapsed:.2f}s — likely ran serially (blocking)"
    )


@pytest.mark.asyncio
async def test_get_account_does_not_block_event_loop(monkeypatch):
    """Same contract for `get_account` — used by the broker health
    pinger which runs on a fixed schedule and must not stall."""
    adapter = _build_stub_adapter(monkeypatch)
    progress = {"ticks": 0}

    async def heartbeat():
        for _ in range(10):
            await asyncio.sleep(0.05)
            progress["ticks"] += 1

    acct_task = asyncio.create_task(adapter.get_account())
    heart_task = asyncio.create_task(heartbeat())
    acct, _ = await asyncio.gather(acct_task, heart_task)
    assert progress["ticks"] >= 8
    assert acct["cash"] == 1000.0


@pytest.mark.asyncio
async def test_sdk_call_isolates_sync_function_on_executor(monkeypatch):
    """Direct smoke test of `_sdk_call`. Pure async behavior — the
    helper must `await` the executor result and return whatever the
    sync function returned."""
    adapter = _build_stub_adapter(monkeypatch)

    def _sync_fn(a, b, *, c=1):
        return a + b + c

    result = await adapter._sdk_call(_sync_fn, 2, 3, c=10)
    assert result == 15
