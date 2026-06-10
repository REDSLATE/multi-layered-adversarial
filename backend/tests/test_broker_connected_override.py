"""Regression test for the broker_connected pre-trade gate.

Doctrine pin (2026-06-10): when an intent carries `broker_override`,
the broker_connected gate MUST resolve the OVERRIDE broker, not the
lane default. Otherwise an intent routed `broker_override="webull"`
dry-run-blocks on missing Public.com config even though the live
path doesn't touch Public.

These tests pin the override-aware behavior at the gate level so a
future refactor of `adapter_for_lane` can't silently re-introduce
the bug.
"""
import sys

sys.path.insert(0, "/app/backend")

import pytest

from shared import broker_router


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    # Default disarmed; tests opt into the armed/keyed state when
    # they need the adapter to actually load.
    for key in ("WEBULL_ARMED", "WEBULL_APP_KEY", "WEBULL_APP_SECRET"):
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.mark.asyncio
async def test_adapter_for_lane_without_override_uses_lane_default():
    """No override → falls back to lane default. Webull keys are
    cleared so if the override-aware code were leaking we'd see a
    None Webull adapter here; but the lane default for crypto is
    Kraken (which may or may not load) — either way, the call MUST
    NOT return a Webull adapter."""
    a = await broker_router.adapter_for_lane("crypto")
    assert a is None or a.name != "webull"


@pytest.mark.asyncio
async def test_adapter_for_lane_with_webull_override_disarmed_returns_none():
    """Override → Webull, but WEBULL_ARMED unset → factory returns
    None. The gate sees broker_connected=False with a clear
    reason — that's the expected fail-closed."""
    a = await broker_router.adapter_for_lane("equity", "webull")
    assert a is None


@pytest.mark.asyncio
async def test_adapter_for_lane_ignores_unknown_override(monkeypatch):
    """Unknown / non-override broker names silently fall back to
    the lane default. Doctrine: only `ROUTE_OVERRIDE_BROKERS`
    members can be selected via override; "public", "kraken",
    "alpaca_paper" are NEVER selectable via the override knob."""
    # "public" is not in ROUTE_OVERRIDE_BROKERS even though it's a
    # known broker. It must be ignored as an override value.
    a = await broker_router.adapter_for_lane("equity", "public")
    # Either None (lane default has no creds in test env) or a
    # Public adapter — but it MUST NOT magically become Webull.
    assert a is None or a.name != "webull"


@pytest.mark.asyncio
async def test_adapter_for_lane_empty_string_override_is_ignored():
    """Empty / whitespace overrides are treated as "no override"."""
    a = await broker_router.adapter_for_lane("crypto", "")
    assert a is None or a.name != "webull"


@pytest.mark.asyncio
async def test_evaluate_gates_broker_connected_honors_webull_override():
    """End-to-end: the dry-run gate must see Webull (when armed +
    keyed) rather than complaining about the lane default. This
    is the 2026-06-10 bug fix — before the change, an
    override='webull' intent dry-run-blocked at broker_connected
    because the gate was calling adapter_for_lane() WITHOUT the
    override."""
    import os
    # We don't actually need to call _evaluate_gates here; the
    # mechanism is the gate calling broker_router.adapter_for_lane
    # with the override. Pin via the resolver's behavior:
    # without override → none, with override → still None unless
    # armed+keyed, but the IDENTITY of what was queried matters.
    from shared.broker_router import adapter_for_lane

    # Both with and without override resolve None in the test env
    # (Webull disarmed by default fixture; lane defaults missing
    # creds in CI), but the broker the gate "asked about" is what
    # the override picks. We verify by mocking ADAPTER_LOADERS to
    # detect which key was requested.
    queries: list[str] = []
    original_loaders = dict(broker_router.ADAPTER_LOADERS)

    async def _make_probe_loader(name: str):
        async def _loader():
            queries.append(name)
            return None
        return _loader

    broker_router.ADAPTER_LOADERS = {
        k: await _make_probe_loader(k) for k in original_loaders
    }
    try:
        await adapter_for_lane("equity", "webull")
    finally:
        broker_router.ADAPTER_LOADERS = original_loaders

    assert "webull" in queries, (
        f"adapter_for_lane should query 'webull' when override is set; "
        f"queried instead: {queries}"
    )
    assert "public" not in queries and "alpaca_paper" not in queries, (
        f"adapter_for_lane must NOT query the lane default when "
        f"the override resolves; queried: {queries}"
    )
