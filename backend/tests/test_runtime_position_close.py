"""Tripwires — runtime position-close endpoint.

Pins the doctrine:

  1. Auth: any valid X-Runtime-Token unlocks the endpoint.
     No token / wrong token → 401.
  2. The endpoint MUST refuse if there's no open position to close.
  3. The endpoint MUST route through the same intent pipeline (12 gates)
     as a fresh BUY — it is NOT a broker bypass.
  4. Long position → SELL; short position → COVER. No other mapping.
  5. Partial close: fraction 0.5 produces a SELL of qty * 0.5.
  6. The resulting intent is tagged `close_intent=True` for forensics.
  7. Unknown lane / bad fraction → 400 / 422 at the boundary.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from routes.runtime_position_close import (
    CloseIn, _inverse_side, _resolve_runtime_from_token, close_position,
)


pytestmark = [pytest.mark.tripwire]


# ─── pure helpers (no DB) ────────────────────────────────────────


def test_long_position_maps_to_sell():
    assert _inverse_side("long") == "SELL"
    assert _inverse_side("LONG") == "SELL"


def test_short_position_maps_to_cover():
    assert _inverse_side("short") == "COVER"
    assert _inverse_side("SHORT") == "COVER"


def test_unknown_side_raises():
    with pytest.raises(ValueError):
        _inverse_side("flat")


def test_fraction_must_be_positive():
    with pytest.raises(Exception):
        CloseIn(symbol="AAPL", lane="equity", fraction=0.0)


def test_fraction_must_be_le_one():
    with pytest.raises(Exception):
        CloseIn(symbol="AAPL", lane="equity", fraction=1.5)


def test_unknown_lane_rejected_at_schema():
    with pytest.raises(Exception):
        CloseIn(symbol="AAPL", lane="forex", fraction=1.0)


def test_resolve_runtime_from_token_matches_env(monkeypatch):
    monkeypatch.setenv("CAMARO_INGEST_TOKEN", "tw-cam")
    monkeypatch.setenv("REDEYE_INGEST_TOKEN", "tw-red")
    assert _resolve_runtime_from_token("tw-cam") == "camaro"
    assert _resolve_runtime_from_token("tw-red") == "redeye"
    assert _resolve_runtime_from_token("nope") is None


# ─── endpoint behavior ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_requires_token():
    from fastapi import HTTPException
    body = CloseIn(symbol="AAPL", lane="equity")
    with pytest.raises(HTTPException) as exc:
        await close_position(body=body, x_runtime_token=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_close_rejects_bogus_token(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setenv("CAMARO_INGEST_TOKEN", "real-camaro-token")
    body = CloseIn(symbol="AAPL", lane="equity")
    with pytest.raises(HTTPException) as exc:
        await close_position(body=body, x_runtime_token="forged-token")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_close_404_when_no_position(monkeypatch):
    """If the broker has no position for that symbol, refuse with 404."""
    from fastapi import HTTPException
    monkeypatch.setenv("CAMARO_INGEST_TOKEN", "real-camaro-token")
    body = CloseIn(symbol="AAPL", lane="equity")

    fake_adapter = AsyncMock()
    fake_adapter.list_positions = AsyncMock(return_value=[])

    with patch(
        "shared.broker_router.adapter_for_lane",
        new=AsyncMock(return_value=fake_adapter),
    ):
        with pytest.raises(HTTPException) as exc:
            await close_position(body=body, x_runtime_token="real-camaro-token")
    assert exc.value.status_code == 404
    assert "nothing to close" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_close_503_when_broker_disconnected(monkeypatch):
    """Alpaca not connected → 503, not 500."""
    from fastapi import HTTPException
    monkeypatch.setenv("CAMARO_INGEST_TOKEN", "real-camaro-token")
    body = CloseIn(symbol="AAPL", lane="equity")

    with patch(
        "shared.broker_router.adapter_for_lane",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(HTTPException) as exc:
            await close_position(body=body, x_runtime_token="real-camaro-token")
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_close_long_routes_sell_through_gate_chain(monkeypatch):
    """A long position closes as a SELL routed through the SAME intent
    pipeline. The post_intent function is called, NOT the broker
    adapter directly — that's the no-bypass guarantee."""
    monkeypatch.setenv("CAMARO_INGEST_TOKEN", "tw-camaro")

    fake_adapter = AsyncMock()
    fake_adapter.list_positions = AsyncMock(return_value=[{
        "symbol": "AAPL", "side": "long", "qty": 50.0,
        "avg_entry_price": 180.0,
    }])
    captured_intent = {}

    async def fake_post_intent(body, x_runtime_token):
        captured_intent["body"] = body
        captured_intent["token"] = x_runtime_token
        return {"intent_id": "fake-intent-id-001"}

    async def noop_update_one(*args, **kwargs):
        return None

    with patch(
        "shared.broker_router.adapter_for_lane",
        new=AsyncMock(return_value=fake_adapter),
    ), patch("shared.intents.post_intent", new=fake_post_intent), patch(
        "routes.runtime_position_close.db",
        new={"shared_intents": AsyncMock(update_one=noop_update_one)},
    ):
        body = CloseIn(symbol="AAPL", lane="equity")
        result = await close_position(body=body, x_runtime_token="tw-camaro")

    assert result["close_action"] == "SELL"
    assert result["close_qty"] == 50.0
    assert result["underlying_side"] == "long"
    assert result["routed_through_gate_chain"] is True
    # The intent that got submitted carried the SELL + the closing brain
    intent_body = captured_intent["body"]
    assert intent_body.stack == "camaro"
    assert intent_body.action == "SELL"
    assert intent_body.symbol == "AAPL"
    assert intent_body.lane == "equity"


@pytest.mark.asyncio
async def test_close_short_routes_cover_through_gate_chain(monkeypatch):
    """A short position closes as a COVER."""
    monkeypatch.setenv("REDEYE_INGEST_TOKEN", "tw-redeye")

    fake_adapter = AsyncMock()
    fake_adapter.list_positions = AsyncMock(return_value=[{
        "symbol": "TSLA", "side": "short", "qty": 20.0,
        "avg_entry_price": 230.0,
    }])
    captured = {}

    async def fake_post_intent(body, x_runtime_token):
        captured["body"] = body
        return {"intent_id": "fake-cover-id-002"}

    async def noop_update_one(*args, **kwargs):
        return None

    with patch(
        "shared.broker_router.adapter_for_lane",
        new=AsyncMock(return_value=fake_adapter),
    ), patch("shared.intents.post_intent", new=fake_post_intent), patch(
        "routes.runtime_position_close.db",
        new={"shared_intents": AsyncMock(update_one=noop_update_one)},
    ):
        body = CloseIn(symbol="TSLA", lane="equity")
        result = await close_position(body=body, x_runtime_token="tw-redeye")

    assert result["close_action"] == "COVER"
    assert result["underlying_side"] == "short"
    assert captured["body"].action == "COVER"


@pytest.mark.asyncio
async def test_partial_close_halves_qty(monkeypatch):
    """fraction=0.5 → close half the qty."""
    monkeypatch.setenv("ALPHA_INGEST_TOKEN", "tw-alpha")

    fake_adapter = AsyncMock()
    fake_adapter.list_positions = AsyncMock(return_value=[{
        "symbol": "NVDA", "side": "long", "qty": 100.0,
        "avg_entry_price": 500.0,
    }])

    async def fake_post_intent(body, x_runtime_token):
        return {"intent_id": "partial-close-id"}

    async def noop_update_one(*args, **kwargs):
        return None

    with patch(
        "shared.broker_router.adapter_for_lane",
        new=AsyncMock(return_value=fake_adapter),
    ), patch("shared.intents.post_intent", new=fake_post_intent), patch(
        "routes.runtime_position_close.db",
        new={"shared_intents": AsyncMock(update_one=noop_update_one)},
    ):
        body = CloseIn(symbol="NVDA", lane="equity", fraction=0.5)
        result = await close_position(body=body, x_runtime_token="tw-alpha")

    assert result["close_qty"] == 50.0
    assert result["underlying_qty"] == 100.0
    assert result["fraction"] == 0.5
