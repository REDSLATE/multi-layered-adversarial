"""Unit tests for `auto_router_helpers.resolve_notional`.

P6b extraction — the 4 doctrine branches must produce the same
notional + source pairs the pre-extraction inline block produced.
"""
from __future__ import annotations

import os

import pytest

from shared.auto_router_helpers import resolve_notional


class TestResolveNotional:

    def test_brain_legacy_sized_wins(self):
        n, src = resolve_notional({
            "action": "BUY", "requested_notional_usd": 42.0,
        })
        assert n == 42.0
        assert src == "brain_legacy"

    def test_brain_v3_sized_wins(self):
        n, src = resolve_notional({
            "action": "BUY",
            "execution": {"notional_usd": 25.0},
        })
        assert n == 25.0
        assert src == "brain_v3"

    def test_micro_probe_when_directional_and_doctrine_failed(self):
        n, src = resolve_notional({
            "action": "BUY",
            "doctrine_packet": {
                "seats": {
                    "execution_judge": {
                        "failed_checks": ["spread_too_wide"]
                    },
                },
            },
        })
        assert src == "micro_probe_failed_quality"
        expected = float(os.environ.get("MICRO_PROBE_FAILED_QUALITY_USD", "1.00"))
        assert n == expected

    def test_micro_default_when_directional_and_doctrine_clean(self):
        n, src = resolve_notional({
            "action": "SELL",
            "doctrine_packet": {"seats": {"execution_judge": {"failed_checks": []}}},
        })
        assert src == "micro_default"
        expected = float(os.environ.get("MICRO_LIVE_DEFAULT_USD", "5.00"))
        assert n == expected

    def test_env_default_when_non_directional(self):
        from shared.auto_router_helpers import AUTO_ROUTER_NOTIONAL_USD
        n, src = resolve_notional({"action": "HOLD"})
        assert n == AUTO_ROUTER_NOTIONAL_USD
        assert src == "env_default"

    def test_brain_v3_wins_over_micro_default(self):
        """Even if the brain is directional AND doctrine is clean,
        an explicit brain size wins."""
        n, src = resolve_notional({
            "action": "BUY",
            "execution": {"notional_usd": 100.0},
            "doctrine_packet": {"seats": {"execution_judge": {"failed_checks": []}}},
        })
        assert n == 100.0
        assert src == "brain_v3"

    def test_zero_size_is_treated_as_missing(self):
        """A 0 notional is NOT a valid brain size — it should fall
        through to micro/env defaults."""
        n, src = resolve_notional({
            "action": "BUY",
            "requested_notional_usd": 0,
            "execution": {"notional_usd": 0.0},
        })
        # Doctrine packet absent → treats as failed_checks=[] → micro_default.
        assert src == "micro_default"

    def test_doctrine_packet_malformed_fails_safe_to_micro_default(self):
        n, src = resolve_notional({
            "action": "BUY",
            "doctrine_packet": "not-a-dict",
        })
        assert src == "micro_default"


class TestRouteContext:

    def test_route_context_default_construction(self):
        from shared.auto_router_helpers import RouteContext
        intent = {"action": "BUY", "symbol": "AAPL"}
        ctx = RouteContext(intent=intent, action_upper="BUY")
        assert ctx.intent is intent
        assert ctx.action_upper == "BUY"
        assert ctx.notional_raw == 0.0
        assert ctx.notional_source == ""
        assert ctx.notional_usd is None
        assert ctx.sd is None
        assert ctx.rc is None
        assert ctx.broker_response == {}
        assert ctx.terminal_state is None
        assert ctx.reason_trail == []

    def test_route_context_reason_trail_is_instance_local(self):
        """Regression guard: default_factory=list must give each
        instance its own list. Class-level [] would leak state."""
        from shared.auto_router_helpers import RouteContext
        a = RouteContext(intent={})
        b = RouteContext(intent={})
        a.reason_trail.append("MASTER_SWITCH_DISARMED")
        assert b.reason_trail == []
