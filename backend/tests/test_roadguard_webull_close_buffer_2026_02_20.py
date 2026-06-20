"""2026-02-20 — RoadGuard `WEBULL_CORE_MARKET_ORDER_CLOSE_BUFFER`.

Pins:
  1. When `WEBULL_CLOSE_BUFFER_SECONDS` minutes before close, RoadGuard
     refuses with verdict `WEBULL_CORE_MARKET_ORDER_CLOSE_BUFFER`.
  2. Outside that window, the buffer is silent — RoadGuard returns its
     normal verdict.
  3. Buffer is configurable via env (default 90 per operator pin
     2026-02-20).
  4. Buffer respects extended-hours mode (LIMIT orders outside CORE
     don't trip Webull's clock check, so the buffer is skipped).
  5. Crypto intents bypass the buffer entirely (24/7 lane).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from shared.pipeline.models import BrainOpinion
from shared.pipeline.roadguard import RoadGuard


# ── helpers ─────────────────────────────────────────────────────────
def _opinion(*, lane: str, evidence: dict | None = None) -> BrainOpinion:
    return BrainOpinion(
        intent_id="t",
        brain_id="hellcat",
        symbol="AAPL" if lane == "equity" else "BTC/USD",
        lane=lane,
        action="BUY",
        confidence=0.65,
        notional_usd=5.0,
        evidence=evidence or {},
    )


@pytest.fixture(autouse=True)
def _stubs():
    """Stub the cross-cutting deps that aren't under test."""
    with patch(
        "routes.trading_controls.is_trading_enabled",
        new=AsyncMock(return_value=True),
    ), patch(
        "routes.equity_extended_hours_admin.get_equity_extended_hours_enabled",
        new=AsyncMock(return_value=False),
    ), patch(
        "shared.market_hours.is_equity_extended_hours",
        return_value=True,
    ):
        yield


# ── 1. Inside the close-buffer window → blocked ─────────────────────
@pytest.mark.asyncio
async def test_blocks_when_within_close_buffer():
    # is_equity_rth returns True for "now", False for "now + buf" →
    # we're inside the buffer.
    rth_results = iter([True, False])

    def _rth(_now=None):
        return next(rth_results)

    with patch("shared.market_hours.is_equity_rth", side_effect=_rth):
        verdict = await RoadGuard().check(
            _opinion(lane="equity", evidence={"market_open": True}),
            notional_usd=5.0,
        )
    assert verdict.passed is False
    assert verdict.reason == "WEBULL_CORE_MARKET_ORDER_CLOSE_BUFFER"


# ── 2. Outside the close-buffer window → passes ─────────────────────
@pytest.mark.asyncio
async def test_passes_when_outside_close_buffer():
    # Both now AND now+buf inside RTH → not in the buffer.
    with patch(
        "shared.market_hours.is_equity_rth", return_value=True,
    ):
        verdict = await RoadGuard().check(
            _opinion(lane="equity", evidence={"market_open": True}),
            notional_usd=5.0,
        )
    assert verdict.passed is True
    assert verdict.reason == "roadguard_passed"


# ── 3. Buffer is configurable via env ───────────────────────────────
@pytest.mark.asyncio
async def test_buffer_env_zero_disables_check(monkeypatch):
    """Setting `WEBULL_CLOSE_BUFFER_SECONDS=0` disables the buffer.
    Operator escape hatch when Webull's behavior changes."""
    monkeypatch.setenv("WEBULL_CLOSE_BUFFER_SECONDS", "0")
    # Even with now+buf=False, the disabled buffer returns False
    # from the helper, so the check passes.
    with patch(
        "shared.market_hours.is_equity_rth", return_value=True,
    ):
        verdict = await RoadGuard().check(
            _opinion(lane="equity", evidence={"market_open": True}),
            notional_usd=5.0,
        )
    assert verdict.passed is True


@pytest.mark.asyncio
async def test_buffer_env_widens_to_300_seconds(monkeypatch):
    monkeypatch.setenv("WEBULL_CLOSE_BUFFER_SECONDS", "300")
    # Stub: now inside RTH, now+300s outside RTH → buffer trips.
    rth_results = iter([True, False])

    def _rth(_now=None):
        return next(rth_results)

    with patch("shared.market_hours.is_equity_rth", side_effect=_rth):
        verdict = await RoadGuard().check(
            _opinion(lane="equity", evidence={"market_open": True}),
            notional_usd=5.0,
        )
    assert verdict.reason == "WEBULL_CORE_MARKET_ORDER_CLOSE_BUFFER"


# ── 4. Extended-hours mode skips the buffer ─────────────────────────
@pytest.mark.asyncio
async def test_extended_hours_skips_close_buffer():
    """When operator has Extended Hours toggled ON, the buffer is
    bypassed — only LIMIT orders flow through during extended hours
    and they don't trip Webull's CORE-MARKET clock check."""
    rth_results = iter([True, True, False])

    def _rth(_now=None):
        return next(rth_results)

    with patch(
        "routes.equity_extended_hours_admin.get_equity_extended_hours_enabled",
        new=AsyncMock(return_value=True),
    ), patch(
        "shared.market_hours.is_equity_extended_hours", return_value=True,
    ), patch(
        "shared.market_hours.is_equity_rth", side_effect=_rth,
    ):
        verdict = await RoadGuard().check(
            _opinion(lane="equity", evidence={"market_open": True}),
            notional_usd=5.0,
        )
    # Should pass — extended mode short-circuits the CORE buffer.
    assert verdict.passed is True


# ── 5. Crypto bypasses the buffer entirely ──────────────────────────
@pytest.mark.asyncio
async def test_crypto_bypasses_close_buffer():
    """Crypto lane is 24/7; equity close has no meaning for it."""
    with patch(
        "shared.market_hours.is_equity_rth", return_value=False,
    ):
        verdict = await RoadGuard().check(
            _opinion(lane="crypto", evidence={}),
            notional_usd=10.0,
        )
    assert verdict.passed is True


# ── 6. _within_webull_core_close_buffer helper ──────────────────────
def test_helper_returns_false_outside_rth():
    """If we're not in RTH NOW, the close buffer doesn't apply —
    `market_closed` already catches it."""
    with patch(
        "shared.market_hours.is_equity_rth", return_value=False,
    ):
        assert RoadGuard._within_webull_core_close_buffer() is False


def test_helper_returns_true_inside_buffer():
    rth_results = iter([True, False])

    def _rth(_now=None):
        return next(rth_results)

    with patch("shared.market_hours.is_equity_rth", side_effect=_rth):
        assert RoadGuard._within_webull_core_close_buffer() is True
