"""Webull-only equity routing tests (2026-02-19).

Operator decision: Public.com and Alpaca paper are DEPRECATED from
the equity lane. Webull is the sole equity broker (parallel route on
crypto). Verify:

  * `LANE_BROKER_REGISTRY["equity"] == "webull"`
  * `_get_equity_adapter()` calls Webull's loader, never Alpaca's
  * If Webull credentials are missing, equity NO_TRADEs (no silent
    Public/Alpaca fallback).

This file replaces the pre-deprecation `test_equity_public_only.py`
doctrine. The OLD contract is removed by design — we want a tripwire
that fires loudly if anyone tries to re-introduce Public/Alpaca as
the equity default.
"""
from __future__ import annotations

import inspect

import pytest

from shared import broker_router as br
from shared.broker_symbol_resolver import LANE_BROKER_REGISTRY


def test_equity_lane_registered_to_webull():
    assert LANE_BROKER_REGISTRY["equity"] == "webull"


def test_get_equity_adapter_never_calls_alpaca():
    """Source-level tripwire — the equity adapter resolver MUST NOT
    reference `get_alpaca_adapter`. A regression that adds an Alpaca
    fallback would silently undo the operator's choice to drop
    Alpaca."""
    src = inspect.getsource(br._get_equity_adapter)
    assert "get_alpaca_adapter" not in src, (
        "_get_equity_adapter must not call Alpaca — Webull is the "
        "sole equity broker."
    )


def test_get_equity_adapter_never_calls_public():
    """Tripwire — Public.com is deprecated. The equity resolver must
    NOT route through `_get_public_adapter` anymore."""
    src = inspect.getsource(br._get_equity_adapter)
    assert "_get_public_adapter" not in src, (
        "_get_equity_adapter must not call Public — Webull is the "
        "sole equity broker."
    )
    assert "get_webull_adapter" in src


@pytest.mark.asyncio
async def test_get_equity_adapter_returns_none_when_webull_down(monkeypatch):
    """If Webull's loader returns None (no creds / probe failed),
    `_get_equity_adapter` must also return None — no silent fallback
    to Public/Alpaca. The router then raises BrokerRouteBlocked →
    NO_TRADE."""
    async def _webull_unavailable():
        return None
    monkeypatch.setattr(br, "get_webull_adapter", _webull_unavailable)
    result = await br._get_equity_adapter()
    assert result is None
