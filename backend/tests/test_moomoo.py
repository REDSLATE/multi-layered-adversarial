"""Moomoo integration tests (2026-06) — config, symbol normalization,
fail-closed loader, options guard, telemetry schema."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.broker.moomoo_adapter import (  # noqa: E402
    LIMITS_DEFAULTS, _plain, _us, configured_summary, moomoo_config,
)

pytestmark = pytest.mark.tripwire


def test_symbol_normalization():
    assert _us("AAPL") == "US.AAPL"
    assert _us("US.AAPL") == "US.AAPL"
    assert _plain("US.AAPL") == "AAPL"


def test_unconfigured_reports_missing_env(monkeypatch):
    monkeypatch.delenv("OPEND_HOST", raising=False)
    assert moomoo_config() is None
    s = configured_summary()
    assert s["configured"] is False
    assert "OPEND_HOST" in s["missing"]


def test_config_from_env_only(monkeypatch):
    monkeypatch.setenv("OPEND_HOST", "10.0.0.5")
    monkeypatch.setenv("MOOMOO_TRADING_ENV", "REAL")
    monkeypatch.setenv("MOOMOO_TRADE_PASSWORD_MD5", "a" * 32)
    cfg = moomoo_config()
    assert cfg["host"] == "10.0.0.5" and cfg["port"] == 11111
    assert cfg["env"] == "REAL"
    s = configured_summary()
    assert s["configured"] and s["unlock_ready"]
    # secrets never surface in the summary payload
    assert "a" * 32 not in str(s)


def test_v1_limits_defaults_are_tiny_and_safe():
    assert LIMITS_DEFAULTS["max_notional_usd"] <= 25.0
    assert LIMITS_DEFAULTS["one_position_at_a_time"] is True
    assert LIMITS_DEFAULTS["rth_only"] is True
    assert LIMITS_DEFAULTS["allow_autonomous_options"] is False


@pytest.mark.asyncio
async def test_loader_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.delenv("OPEND_HOST", raising=False)
    from shared.broker.moomoo_adapter import get_moomoo_adapter
    assert await get_moomoo_adapter() is None  # router → BrokerRouteBlocked


def test_router_and_selection_wiring():
    br = open("/app/backend/shared/broker_router.py").read()
    assert '"moomoo": _get_moomoo_adapter_loader' in br
    sel = open("/app/backend/routes/broker_selection.py").read()
    assert '"moomoo"' in sel
