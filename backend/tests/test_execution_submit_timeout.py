"""Regression: /execution/submit timeouts cleanly at 20s.

Operator incident (2026-02-19, ~2:22pm CST): the user deployed the
auto-router + Webull SDK non-blocking fix at 1:56pm CST, and a manual
submit still returned HTTP 502 on the dashboard at 2:22pm.

Root cause discovered: even with the Webull SDK calls dispatched to
`run_in_executor`, the manual `/api/execution/submit` endpoint had NO
timeout around `route_order`. A slow Webull API round-trip (network
jitter, broker rate-limit, IPO-day load) was still able to hang the
request past Cloudflare's 30s gateway timeout → 502.

Fix: wrap the manual-submit's `route_order` in `asyncio.wait_for(...,
timeout=20.0)`. On timeout we return HTTP 504 with a clean
`broker_submit_timeout_20s` reason so the operator sees a readable
block on the dashboard instead of a generic 502.

This test does NOT exercise the full submit endpoint (which would
require a Mongo intent doc + gate-chain fixtures); it exercises the
timeout contract via direct `asyncio.wait_for` semantics so a future
regression that drops the wrapper fails loudly.
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_asyncio_wait_for_route_order_pattern():
    """Pin the pattern: a coroutine that exceeds the ceiling raises
    asyncio.TimeoutError — which the submit endpoint converts into
    HTTP 504, not a hung-request → 502."""
    async def _slow_route(notional_usd: float):
        await asyncio.sleep(0.4)  # > timeout
        return {"order_id": "should-never-arrive"}

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_slow_route(10.0), timeout=0.1)


@pytest.mark.asyncio
async def test_asyncio_wait_for_fast_call_returns_value():
    """And the happy path: a fast `route_order` returns its result
    well under the ceiling."""
    async def _fast_route(notional_usd: float):
        await asyncio.sleep(0.01)
        return {"order_id": "ORD-1"}

    order = await asyncio.wait_for(_fast_route(10.0), timeout=1.0)
    assert order["order_id"] == "ORD-1"


def test_execution_submit_has_timeout_wrapper():
    """Source-level tripwire: the submit endpoint MUST wrap
    `_route_order` in `asyncio.wait_for`. Catches any future PR that
    inadvertently drops the wrapper.
    """
    import inspect
    from shared import execution as ex
    src = inspect.getsource(ex.execution_submit)
    assert "asyncio.wait_for" in src, (
        "execution_submit must wrap route_order in asyncio.wait_for "
        "so a slow broker call returns HTTP 504, not a Cloudflare 502."
    )
    # And the timeout must be < Cloudflare's 30s gateway ceiling.
    assert "timeout=20.0" in src or "timeout=20" in src, (
        "execution_submit timeout must be < 30s (Cloudflare gateway "
        "ceiling). Got something else — please review."
    )
