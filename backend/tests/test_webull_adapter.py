"""Tests for the Webull broker adapter (structural + symbol lane).

The adapter wraps the official Webull OpenAPI Python SDK. We do NOT
hit the live SDK in unit tests — that would burn the operator's
real Webull account. Instead we test:

  * `get_webull_adapter()` factory returns None when keys are missing
    OR when WEBULL_ARMED!=true (fail-closed default).
  * The adapter's symbol→lane index correctly classifies equity vs
    crypto (so `submit_market_order` builds the right SDK payload).
  * `submit_market_order` re-checks the armed gate and cap band as
    belt-and-braces, and refuses to leave the adapter when either
    is violated.
  * `submit_market_order` raises if both qty and notional are given,
    or if neither is supplied.
"""
import sys

sys.path.insert(0, "/app/backend")

import pytest

from shared.broker.webull import (
    WebullAdapter,
    _lane_for_symbol,
    get_webull_adapter,
)
from shared.broker.webull_caps import WebullCapBlocked


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for key in (
        "WEBULL_ARMED",
        "WEBULL_APP_KEY",
        "WEBULL_APP_SECRET",
        "WEBULL_REGION_ID",
        "WEBULL_ENVIRONMENT",
        "WEBULL_MIN_NOTIONAL_USD",
        "WEBULL_MAX_NOTIONAL_USD",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


# ── symbol lane classification ─────────────────────────────────────


def test_lane_for_known_equity():
    assert _lane_for_symbol("AAPL") == "equity"
    assert _lane_for_symbol("aapl") == "equity"  # case-insensitive


def test_lane_for_known_crypto():
    assert _lane_for_symbol("BTCUSD") == "crypto"
    assert _lane_for_symbol("ETHUSD") == "crypto"


def test_lane_for_unknown_symbol():
    assert _lane_for_symbol("ZZZZ") is None


# ── factory: get_webull_adapter ────────────────────────────────────


@pytest.mark.asyncio
async def test_factory_returns_none_without_keys():
    a = await get_webull_adapter()
    assert a is None


@pytest.mark.asyncio
async def test_factory_returns_none_when_disarmed(monkeypatch):
    monkeypatch.setenv("WEBULL_APP_KEY", "k")
    monkeypatch.setenv("WEBULL_APP_SECRET", "s")
    monkeypatch.delenv("WEBULL_ARMED", raising=False)
    a = await get_webull_adapter()
    assert a is None, "must NOT build a live adapter when WEBULL_ARMED is unset"


@pytest.mark.asyncio
async def test_factory_returns_none_when_only_key(monkeypatch):
    monkeypatch.setenv("WEBULL_APP_KEY", "k")
    monkeypatch.setenv("WEBULL_ARMED", "true")
    # missing secret
    a = await get_webull_adapter()
    assert a is None


# ── adapter belt-and-braces gates ──────────────────────────────────


class _StubApiClient:
    """Minimal stand-in for webull.core.client.ApiClient. We never
    let calls reach it because all of these tests stop at the gate."""
    def add_endpoint(self, *_args, **_kwargs):
        pass


def _adapter() -> WebullAdapter:
    return WebullAdapter(api_client=_StubApiClient(), account_id="ACC123")


@pytest.mark.asyncio
async def test_submit_blocked_when_not_armed():
    """Even if the router somehow constructed an adapter while
    WEBULL_ARMED=false, the adapter must refuse to submit."""
    adapter = _adapter()
    with pytest.raises(WebullCapBlocked) as exc:
        await adapter.submit_market_order("AAPL", notional=5.0, side="BUY")
    assert "NOT_ARMED" in str(exc.value)


@pytest.mark.asyncio
async def test_submit_blocked_below_floor(monkeypatch):
    monkeypatch.setenv("WEBULL_ARMED", "true")
    adapter = _adapter()
    with pytest.raises(WebullCapBlocked) as exc:
        await adapter.submit_market_order("AAPL", notional=2.00, side="BUY")
    assert "BELOW_FLOOR" in str(exc.value)


@pytest.mark.asyncio
async def test_submit_blocked_above_cap(monkeypatch):
    monkeypatch.setenv("WEBULL_ARMED", "true")
    adapter = _adapter()
    with pytest.raises(WebullCapBlocked) as exc:
        await adapter.submit_market_order("AAPL", notional=12.00, side="BUY")
    assert "ABOVE_CAP" in str(exc.value)


@pytest.mark.asyncio
async def test_submit_rejects_both_qty_and_notional(monkeypatch):
    monkeypatch.setenv("WEBULL_ARMED", "true")
    adapter = _adapter()
    with pytest.raises(ValueError):
        await adapter.submit_market_order(
            "AAPL", qty=0.1, notional=5.0, side="BUY",
        )


@pytest.mark.asyncio
async def test_submit_rejects_neither_qty_nor_notional(monkeypatch):
    monkeypatch.setenv("WEBULL_ARMED", "true")
    adapter = _adapter()
    with pytest.raises(ValueError):
        await adapter.submit_market_order("AAPL", side="BUY")


@pytest.mark.asyncio
async def test_submit_rejects_unknown_symbol_lane(monkeypatch):
    """If the resolver hands us a symbol we don't know how to classify
    (equity vs crypto), refuse the order. Belt-and-braces against a
    broken broker_symbol_resolver update."""
    monkeypatch.setenv("WEBULL_ARMED", "true")
    adapter = _adapter()
    with pytest.raises(RuntimeError) as exc:
        await adapter.submit_market_order("ZZZUNKNOWN", notional=5.0, side="BUY")
    assert "no lane known" in str(exc.value)


# ── adapter contract surface ───────────────────────────────────────


def test_adapter_name_and_paper_flag():
    a = _adapter()
    assert a.name == "webull"
    assert a.is_paper is False  # live trading, operator-pinned


@pytest.mark.asyncio
async def test_submit_limit_order_not_wired(monkeypatch):
    """Limit orders are deliberately out of scope for the small-pilot
    band. Calling submit_limit_order must raise NotImplementedError
    so a code path that wandered in fails loudly."""
    monkeypatch.setenv("WEBULL_ARMED", "true")
    adapter = _adapter()
    with pytest.raises(NotImplementedError):
        await adapter.submit_limit_order(
            "AAPL", qty=0.1, limit_price=150.0, side="BUY",
        )
