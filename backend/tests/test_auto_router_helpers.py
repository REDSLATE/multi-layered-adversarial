"""Characterization tests for the pure helpers extracted from
`shared.auto_router._route_one` during the 2026-05-17 refactor.

These pin the COMPUTATIONAL bits of the auto-router (lane-clamp,
side-from-action, effective notional, receipt builder, response shape)
so the refactor cannot drift behavior. Async DB-touching helpers are
left to integration coverage.
"""
from __future__ import annotations

import pytest

from shared.auto_router import (
    AUTO_ROUTER_EMAIL,
    _blocked_response,
    _build_receipt,
    _clamp_notional_to_lane,
    _effective_notional,
    _side_for_action,
)


# Tripwire suite: locked auto-router helper behavior. See pytest.ini.
pytestmark = pytest.mark.tripwire


# ── lane clamp ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("lane,notional,expected_clamped", [
    # crypto cap = $500 (operator-chosen 2026-05-18). Generous enough
    # for normal sizing; tight enough that a brain bug emitting a huge
    # notional can't blow up the account. Live routing is gated by
    # seat policy; the cap is operational insurance only.
    ("crypto", 100.0, 100.0),  # below cap → unchanged
    ("crypto", 500.0, 500.0),  # at cap → unchanged
    ("crypto", 1000.0, 500.0),  # over crypto cap → clamps
    ("crypto", 25.0,  25.0),
    ("equity", 100.0, 100.0),  # equity cap is $100k → unchanged
    ("equity", 50.0,  50.0),
    (None,     100.0, 100.0),  # no lane → falls back to global per-order cap
])
def test_clamp_notional_to_lane(lane, notional, expected_clamped):
    clamped, was_clamped = _clamp_notional_to_lane(notional, lane)
    assert clamped == expected_clamped
    assert was_clamped == (clamped != notional)


# ── side derivation ─────────────────────────────────────────────────────

@pytest.mark.parametrize("action,expected", [
    ("BUY",   "BUY"),
    ("COVER", "BUY"),    # closes a short — buy-side
    ("SELL",  "SELL"),
    ("SHORT", "SELL"),
    ("HOLD",  "SELL"),   # legacy default: anything non-BUY/COVER → SELL
])
def test_side_for_action(action, expected):
    assert _side_for_action(action) == expected


# ── effective notional ──────────────────────────────────────────────────

@pytest.mark.parametrize("base,risk,expected", [
    (100.0, 1.0,  100.0),
    (100.0, 0.5,  50.0),
    (100.0, 0.0,  100.0),  # zero multiplier falls back to base (paranoia)
    (50.0,  2.0,  100.0),  # quantum upweight scenario
    (30.0,  0.75, 22.5),
])
def test_effective_notional(base, risk, expected):
    assert _effective_notional(base, risk) == pytest.approx(expected)


# ── blocked response ────────────────────────────────────────────────────

def test_blocked_response_picks_first_failing_gate():
    gates = [
        {"name": "schema_invariants", "passed": True, "reason": "ok"},
        {"name": "executor_seat_check", "passed": False, "reason": "seat vacant"},
        {"name": "broker_connected", "passed": False, "reason": "no adapter"},
    ]
    resp = _blocked_response("intent-abc", gates)
    assert resp == {
        "intent_id": "intent-abc",
        "verdict": "blocked",
        "reason": "seat vacant",
    }


def test_blocked_response_falls_back_when_no_failures():
    # Edge case: should never happen, but be defensive.
    resp = _blocked_response("intent-xyz", [
        {"name": "x", "passed": True, "reason": "ok"},
    ])
    assert resp["verdict"] == "blocked"
    assert resp["reason"] == "gate chain blocked"


# ── receipt builder ─────────────────────────────────────────────────────

def test_build_receipt_full_shape():
    intent = {"intent_id": "i1", "stack": "alpha", "symbol": "AAPL", "action": "BUY"}
    order = {
        "order_id": "alp-123", "client_order_id": "ar-i1-aaa",
        "status": "submitted", "submitted_at": "2026-05-17T00:00:00+00:00",
        "filled_at": None, "filled_qty": 0.0, "filled_avg_price": None,
        "broker": "alpaca_paper", "canonical": "EQ:AAPL", "lane": "equity",
        "broker_symbol": "AAPL",
    }
    r = _build_receipt(
        intent=intent, order=order, side="BUY",
        effective_notional=80.0, requested_notional=100.0,
        risk_multiplier=0.8, gates=[{"name": "x", "passed": True}],
        now_iso="2026-05-17T00:00:01+00:00",
    )
    # Critical fields the executor-receipt schema requires.
    assert r["intent_id"] == "i1"
    assert r["broker_order_id"] == "alp-123"
    assert r["client_order_id"] == "ar-i1-aaa"
    assert r["broker"] == "alpaca_paper"
    assert r["canonical"] == "EQ:AAPL"
    assert r["lane"] == "equity"
    assert r["broker_symbol"] == "AAPL"
    assert r["side"] == "BUY"
    assert r["notional_usd"] == 80.0
    assert r["requested_notional_usd"] == 100.0
    assert r["risk_multiplier"] == 0.8
    assert r["executed_by"] == AUTO_ROUTER_EMAIL
    assert r["auto_routed"] is True
    assert isinstance(r["receipt_id"], str) and len(r["receipt_id"]) > 8
    assert r["executed_at"] == "2026-05-17T00:00:01+00:00"
    # submitted_at falls back to now_iso when missing.
    r2 = _build_receipt(
        intent=intent, order={**order, "submitted_at": None},
        side="BUY", effective_notional=80.0, requested_notional=100.0,
        risk_multiplier=0.8, gates=[], now_iso="2026-05-17T00:00:01+00:00",
    )
    assert r2["submitted_at"] == "2026-05-17T00:00:01+00:00"
