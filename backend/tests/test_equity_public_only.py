"""Public-only equity routing tests (2026-02-XX).

Operator decision: Alpaca paper removed from equity lane. Public.com
is the sole equity broker. Verify:

  * `equity_broker_preference()` always returns "public"
  * `_get_equity_adapter()` calls Public's loader, never Alpaca's
  * If Public is unavailable, equity NO_TRADEs (no silent Alpaca
    fallback)
"""
from __future__ import annotations

import inspect

import pytest

from shared import broker_router as br
from shared.broker_symbol_resolver import equity_broker_preference


def test_preference_returns_public_by_default(monkeypatch):
    monkeypatch.delenv("RISEDUAL_EQUITY_BROKER", raising=False)
    assert equity_broker_preference() == "public"


def test_preference_returns_public_for_legacy_values(monkeypatch):
    """Stale env vars like `auto` or `alpaca_paper` from earlier
    deploys must NOT silently route through Alpaca anymore."""
    for legacy in ("auto", "alpaca_paper", "alpaca", "garbage"):
        monkeypatch.setenv("RISEDUAL_EQUITY_BROKER", legacy)
        assert equity_broker_preference() == "public", (
            f"legacy value {legacy!r} should resolve to `public`, not {legacy!r}"
        )


def test_get_equity_adapter_never_calls_alpaca():
    """Source-level tripwire — the equity adapter resolver MUST NOT
    reference `get_alpaca_adapter`. A regression that adds an Alpaca
    fallback would silently undo the operator's choice to drop
    Alpaca."""
    src = inspect.getsource(br._get_equity_adapter)
    assert "get_alpaca_adapter" not in src, (
        "_get_equity_adapter must not call Alpaca — Public is the "
        "sole equity broker."
    )
    assert "_get_public_adapter" in src


@pytest.mark.asyncio
async def test_get_equity_adapter_returns_none_when_public_down(monkeypatch):
    """If Public's loader returns None (no creds / probe failed),
    `_get_equity_adapter` must also return None — no silent Alpaca
    fallback. The router then raises BrokerRouteBlocked → NO_TRADE."""
    async def _public_unavailable():
        return None
    monkeypatch.setattr(br, "_get_public_adapter", _public_unavailable)
    result = await br._get_equity_adapter()
    assert result is None
