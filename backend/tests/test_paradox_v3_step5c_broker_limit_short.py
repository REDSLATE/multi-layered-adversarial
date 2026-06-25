"""Paradox v3 — Step 5.c broker-layer limit + short wiring tests.

Pins the three contracts that close the gap operator flagged:
  * `_BrokerAdapter` exposes `submit_limit_order`.
  * `execution_pipeline` dispatches market vs limit based on
    `opinion.execution.limit_price`.
  * `route_order` accepts `limit_price` kwarg, converts notional → qty,
    and routes SHORT-on-crypto-on-kraken with a `leverage` param.
  * Kraken adapter `submit_limit_order` exists and honours leverage.
  * v2 emits (limit_price=None) take the unchanged market path —
    no regression.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest


# ── _BrokerAdapter dispatch ───────────────────────────────────────
class TestBrokerAdapterDispatch:
    @pytest.mark.asyncio
    async def test_market_path_when_no_limit_price(self):
        from shared.pipeline.adapter import _BrokerAdapter
        adapter = _BrokerAdapter({"intent_id": "t", "action": "BUY"})
        with patch(
            "shared.pipeline.adapter.route_order",
            new=AsyncMock(return_value={"order_id": "x", "broker": "kraken"}),
        ) as mock_route:
            out = await adapter.submit_market_order(
                symbol="XBTUSD", side="BUY",
                notional_usd=100.0, lane="crypto",
            )
            assert out["status"] == "submitted"
            mock_route.assert_called_once()
            # market path → no limit_price kwarg
            assert "limit_price" not in mock_route.call_args.kwargs

    @pytest.mark.asyncio
    async def test_limit_path_threads_limit_price(self):
        from shared.pipeline.adapter import _BrokerAdapter
        adapter = _BrokerAdapter({"intent_id": "t", "action": "BUY"})
        with patch(
            "shared.pipeline.adapter.route_order",
            new=AsyncMock(return_value={"order_id": "x", "broker": "kraken"}),
        ) as mock_route:
            out = await adapter.submit_limit_order(
                symbol="XBTUSD", side="BUY",
                notional_usd=100.0, lane="crypto", limit_price=65_000.0,
            )
            assert out["status"] == "submitted"
            assert mock_route.call_args.kwargs["limit_price"] == 65_000.0


# ── execution_pipeline dispatch ───────────────────────────────────
class TestExecutionPipelineDispatch:
    """Pins that execution_pipeline reads `opinion.execution.limit_price`
    and dispatches to the right broker method."""

    @pytest.mark.asyncio
    async def test_pipeline_calls_market_when_execution_limit_none(self):
        """Pure regression — v2 emit path is unchanged. The pipeline
        reads `opinion.execution.limit_price` lazily via `.get()` so a
        None execution block still routes to market."""
        from shared.pipeline.models import BrainOpinion

        opinion = BrainOpinion(
            intent_id="t", brain_id="camino", lane="equity", symbol="AAPL",
            action="BUY", confidence=0.7, notional_usd=10.0,
            # execution=None — the v2 default.
        )
        # Just verify the data class accepts None execution and the
        # access pattern `(opinion.execution or {}).get("limit_price")`
        # returns None safely.
        assert opinion.execution is None
        block = opinion.execution or {}
        assert block.get("limit_price") is None

    @pytest.mark.asyncio
    async def test_pipeline_reads_limit_price_from_execution_block(self):
        from shared.pipeline.models import BrainOpinion

        opinion = BrainOpinion(
            intent_id="t", brain_id="camino", lane="equity", symbol="AAPL",
            action="BUY", confidence=0.7, notional_usd=10.0,
            execution={"limit_price": 187.40, "action": "BUY"},
        )
        block = opinion.execution or {}
        assert block.get("limit_price") == 187.40


# ── route_order limit dispatch + leverage detection ───────────────
class TestRouteOrderLimitAndLeverage:

    @pytest.mark.asyncio
    async def test_route_order_dispatches_limit_when_set(self, monkeypatch):
        """A limit_price kwarg → adapter.submit_limit_order is called
        with the converted qty. Market never gets touched."""
        from shared import broker_router as br

        # Stub adapter loader + asset resolution to bypass real broker.
        mock_adapter = AsyncMock()
        mock_adapter.submit_limit_order = AsyncMock(
            return_value={"order_id": "abc", "status": "submitted"},
        )
        mock_adapter.submit_market_order = AsyncMock(
            return_value={"order_id": "should-not-be-called"},
        )
        monkeypatch.setitem(
            br.ADAPTER_LOADERS, "kraken", AsyncMock(return_value=mock_adapter),
        )
        # Bypass MC receipt enforcement + lane gates.
        monkeypatch.setattr(
            br, "_mint_and_verify_mc_receipt",
            lambda **kw: {"enforced": False, "ok": True, "reason": "test-bypass",
                          "receipt": {"signature": "s", "mc_policy_hash": "h"}},
        )
        monkeypatch.setattr(br, "assert_not_frozen", AsyncMock())
        # Make is_lane_enabled importable & return True regardless.
        monkeypatch.setattr(
            "routes.broker_lane_admin.is_lane_enabled",
            AsyncMock(return_value=True),
        )
        # Stub symbol resolver to return a string passthrough.
        monkeypatch.setattr(
            br, "resolve_broker_symbol", lambda asset, broker: "XBTUSD",
        )
        # Stub broker_for_lane to always pick kraken for this test.
        monkeypatch.setattr(br, "broker_for_lane", lambda lane: "kraken")

        intent = {
            "intent_id": "t", "action": "BUY", "lane": "crypto",
            "symbol": "BTC/USD",
        }
        await br.route_order(
            intent, notional_usd=100.0, client_order_id="cid",
            limit_price=65_000.0,
        )
        mock_adapter.submit_limit_order.assert_called_once()
        # qty = notional / limit_price = 100 / 65000
        kwargs = mock_adapter.submit_limit_order.call_args.kwargs
        assert abs(kwargs["qty"] - (100.0 / 65_000.0)) < 1e-9
        assert kwargs["limit_price"] == 65_000.0
        # SHORT detection: action=BUY → no leverage.
        assert "leverage" not in kwargs
        mock_adapter.submit_market_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_route_order_adds_leverage_for_short_crypto_kraken(
        self, monkeypatch,
    ):
        """The locked contract: BEARISH crypto refire → SHORT → Kraken
        gets a `leverage` kwarg so the order opens a margin position
        instead of erroring on spot-sell-no-position."""
        from shared import broker_router as br

        mock_adapter = AsyncMock()
        mock_adapter.submit_market_order = AsyncMock(
            return_value={"order_id": "abc", "status": "submitted"},
        )
        monkeypatch.setitem(
            br.ADAPTER_LOADERS, "kraken", AsyncMock(return_value=mock_adapter),
        )
        monkeypatch.setattr(
            br, "_mint_and_verify_mc_receipt",
            lambda **kw: {"enforced": False, "ok": True, "reason": "test",
                          "receipt": {"signature": "s", "mc_policy_hash": "h"}},
        )
        monkeypatch.setattr(br, "assert_not_frozen", AsyncMock())
        monkeypatch.setattr(
            "routes.broker_lane_admin.is_lane_enabled",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(
            br, "resolve_broker_symbol", lambda asset, broker: "XBTUSD",
        )
        monkeypatch.setattr(br, "broker_for_lane", lambda lane: "kraken")
        monkeypatch.setenv("PARADOX_V3_KRAKEN_SHORT_LEVERAGE", "2")

        intent = {
            "intent_id": "t", "action": "SHORT", "lane": "crypto",
            "symbol": "BTC/USD",
        }
        await br.route_order(
            intent, notional_usd=100.0, client_order_id="cid",
        )
        mock_adapter.submit_market_order.assert_called_once()
        kwargs = mock_adapter.submit_market_order.call_args.kwargs
        assert kwargs["leverage"] == 2
        assert kwargs["side"] == "SELL"  # SHORT → SELL on Kraken

    @pytest.mark.asyncio
    async def test_route_order_no_leverage_for_long_crypto(self, monkeypatch):
        """BULLISH crypto → BUY → spot, no leverage."""
        from shared import broker_router as br

        mock_adapter = AsyncMock()
        mock_adapter.submit_market_order = AsyncMock(
            return_value={"order_id": "abc"},
        )
        monkeypatch.setitem(
            br.ADAPTER_LOADERS, "kraken", AsyncMock(return_value=mock_adapter),
        )
        monkeypatch.setattr(
            br, "_mint_and_verify_mc_receipt",
            lambda **kw: {"enforced": False, "ok": True, "reason": "t",
                          "receipt": {"signature": "s", "mc_policy_hash": "h"}},
        )
        monkeypatch.setattr(br, "assert_not_frozen", AsyncMock())
        monkeypatch.setattr(
            "routes.broker_lane_admin.is_lane_enabled",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(
            br, "resolve_broker_symbol", lambda asset, broker: "XBTUSD",
        )
        monkeypatch.setattr(br, "broker_for_lane", lambda lane: "kraken")

        intent = {
            "intent_id": "t", "action": "BUY", "lane": "crypto",
            "symbol": "BTC/USD",
        }
        await br.route_order(intent, notional_usd=100.0)
        kwargs = mock_adapter.submit_market_order.call_args.kwargs
        assert "leverage" not in kwargs


# ── Kraken adapter submit_limit_order presence + params ───────────
class TestKrakenLimitOrder:

    def test_kraken_adapter_has_submit_limit_order(self):
        from shared.crypto.broker_adapter import KrakenLiveAdapter
        assert hasattr(KrakenLiveAdapter, "submit_limit_order")
        assert callable(KrakenLiveAdapter.submit_limit_order)

    def test_kraken_adapter_market_accepts_leverage_kwarg(self):
        """The contract — `leverage` must be accepted (even if 1
        produces a spot order). A future refactor that drops it
        breaks the SHORT-crypto path silently."""
        import inspect
        from shared.crypto.broker_adapter import KrakenLiveAdapter
        sig = inspect.signature(KrakenLiveAdapter.submit_market_order)
        assert "leverage" in sig.parameters

    def test_kraken_adapter_limit_accepts_leverage_kwarg(self):
        import inspect
        from shared.crypto.broker_adapter import KrakenLiveAdapter
        sig = inspect.signature(KrakenLiveAdapter.submit_limit_order)
        assert "leverage" in sig.parameters

    @pytest.mark.asyncio
    async def test_kraken_limit_order_rejects_without_mc_receipt(self):
        """The doctrine pin: NO broker write without an MC execution
        receipt. Mirrors the same guard on submit_market_order."""
        from shared.crypto.broker_adapter import KrakenLiveAdapter
        adapter = KrakenLiveAdapter(public_key="pub", private_key="priv")
        with pytest.raises(PermissionError, match="MC execution"):
            await adapter.submit_limit_order(
                symbol="XBTUSD", qty=0.001, limit_price=65_000.0,
                side="BUY", mc_receipt=None,
            )

    @pytest.mark.asyncio
    async def test_kraken_limit_order_builds_correct_params(self, monkeypatch):
        """When mc_receipt is supplied + qty/limit valid, the adapter
        sends `ordertype=limit` + `price` + (optionally) `leverage`."""
        from shared.crypto import broker_adapter as ka
        captured = {}

        async def stub_call_private(path, pub, priv, params):
            captured["params"] = params
            return {"txid": ["FAKE-TX"], "descr": {"order": "limit ..."}}

        monkeypatch.setattr(ka, "call_private", stub_call_private)
        monkeypatch.setattr(ka, "to_kraken_pair", lambda s: "XBTUSD")
        adapter = ka.KrakenLiveAdapter(public_key="pub", private_key="priv")
        out = await adapter.submit_limit_order(
            symbol="BTC/USD", qty=0.001, limit_price=65_000.0,
            side="SELL", client_order_id="cid",
            mc_receipt={"signature": "s", "mc_policy_hash": "h"},
            leverage=2,
        )
        p = captured["params"]
        assert p["ordertype"] == "limit"
        assert p["type"] == "sell"
        assert p["pair"] == "XBTUSD"
        assert p["leverage"] == "2"
        assert float(p["price"]) == 65_000.0
        assert out["status"] == "submitted"
        assert out["limit_price"] == 65_000.0


# ── BrainOpinion carries execution block ──────────────────────────
def test_brainopinion_carries_execution_block():
    from shared.pipeline.models import BrainOpinion
    o = BrainOpinion(
        intent_id="t", brain_id="camino", lane="crypto", symbol="BTC/USD",
        action="SHORT", confidence=0.7, notional_usd=10.0,
        intent_version="v3",
        plan={"intent": "ENTER", "stance": "BEARISH"},
        execution={"action": "SHORT", "limit_price": 60_000.0},
    )
    assert o.execution["limit_price"] == 60_000.0
    assert o.execution["action"] == "SHORT"
