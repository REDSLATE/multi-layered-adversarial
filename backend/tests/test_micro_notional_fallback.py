"""Micro-notional fallback tests for `shared.auto_router._route_one`.

Locks in the 2026-07-09 operator directive:

    if execution.action in {BUY, SELL} and execution.notional_usd is None:
        execution.notional_usd = 5.00
        execution.notional_source = micro_live_default

Notional resolution precedence inside `_route_one`:

    1. `intent.requested_notional_usd`  → notional_source = "brain_legacy"
    2. `intent.execution.notional_usd`  → notional_source = "brain_v3"
    3. directional (BUY/SELL) w/ both None → MICRO_LIVE_DEFAULT_USD (default $5.00)
       → notional_source = "micro_live_default"
    4. anything else (HOLD, etc.)       → AUTO_ROUTER_NOTIONAL_USD ($10)
       → notional_source = "env_default"

These tests reuse the same monkeypatch pattern as
`test_capital_ledger_wiring.py` — the real `_route_one` control flow
runs, but seat/risk/broker are stubbed. We inspect the notional the
mocked broker ends up seeing to determine what got resolved.

FOLLOW-UP GAP (2026-07-09): the current code computes
`notional_source` locally but NEVER persists it to `shared_intents`.
The `test_notional_source_persisted_on_intent_*` tests below assert
persistence and will FAIL until the writer branches also stamp
`notional_source` (and `notional_usd`) on the intent doc.
"""
from __future__ import annotations

import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, "/app/backend")

from db import db  # noqa: E402
from namespaces import CAPITAL_LEDGER, SHARED_INTENTS  # noqa: E402
from shared import auto_router  # noqa: E402
from shared.capital.ledger import init_ledger  # noqa: E402


# ─── Fixtures & helpers ──────────────────────────────────────────


@pytest.fixture(autouse=True)
async def _clean_state():
    await db[CAPITAL_LEDGER].delete_many({})
    await db[SHARED_INTENTS].delete_many(
        {"intent_id": {"$regex": "^micro-notional-test-"}},
    )
    yield
    await db[CAPITAL_LEDGER].delete_many({})
    await db[SHARED_INTENTS].delete_many(
        {"intent_id": {"$regex": "^micro-notional-test-"}},
    )
    # Reset sys.modules-cached `shared.<attr>` — see comment in
    # test_capital_ledger_wiring.py for the rationale.
    import shared as _shared_pkg
    for _attr in ("seat", "risk", "executions"):
        try:
            delattr(_shared_pkg, _attr)
        except AttributeError:
            pass


def _seat_fire(risk_mult=1.0):
    return SimpleNamespace(
        verdict="fire",
        reason="ok",
        risk_multiplier=risk_mult,
        strategist="camino",
        governor="camino",
        executor="camino",
        auditor="camino",
        angels={},
        intent_brain="camino",
        lane="equity",
    )


def _risk_pass_through():
    """Risk mock that returns whatever notional the caller passed in."""
    async def _check(intent, notional_usd):  # noqa: ARG001
        return SimpleNamespace(
            ok=True, reason="ok", notional_usd=notional_usd,
        )
    return _check


async def _insert_intent(intent_id, action, *, legacy=None, v3=None,
                         lane="equity"):
    doc = {
        "intent_id": intent_id,
        "symbol": "AAPL" if lane == "equity" else "BTC/USD",
        "action": action,
        "lane": lane,
        "stack": "camino",
        "stack_canonical": "camino",
        "gate_state": "queued",
    }
    if legacy is not None:
        doc["requested_notional_usd"] = legacy
    if v3 is not None:
        doc["execution"] = {"notional_usd": v3}
    await db[SHARED_INTENTS].insert_one(doc)
    return doc


def _wire_common_patches(monkeypatch, *, sizing_route="observe"):
    """Bolt in the seat/risk/executions/market-hours/sizing/ledger
    stubs. Uses `observe` sizing by default so the ledger reserve
    path is skipped entirely — keeps these tests focused on notional
    resolution, not ledger arithmetic.
    """
    import shared.seat as seat
    import shared.risk as risk
    import shared.executions as executions
    monkeypatch.setattr(seat, "decide", AsyncMock(return_value=_seat_fire()))
    monkeypatch.setattr(risk, "check", _risk_pass_through())
    monkeypatch.setattr(executions, "record", AsyncMock())
    monkeypatch.setattr(
        "shared.sizing_gate.evaluate_sizing_with_ladder",
        AsyncMock(return_value=SimpleNamespace(
            final_usd=0.0, route=sizing_route, stage="observation_only",
            binding_rail="ladder_observation", ladder_cap_usd=0.0,
            execution_mode="observation_only",
        )),
    )
    # Bypass market hours (equity path).
    monkeypatch.setattr("shared.market_hours.is_equity_rth", lambda: True)
    monkeypatch.setattr(
        "shared.market_hours.is_equity_extended_hours", lambda: True,
    )
    monkeypatch.setattr(
        "routes.equity_extended_hours_admin.get_equity_extended_hours_enabled",
        AsyncMock(return_value=False),
    )


def _capture_broker(monkeypatch):
    """Patch `route_order` to record the notional it receives.
    Returns a `captured` dict — after `_route_one` completes, the
    resolved notional is at `captured["notional"]`.
    """
    captured: dict = {}

    async def _fake_route_order(intent, notional_usd, client_order_id):  # noqa: ARG001
        captured["notional"] = notional_usd
        return {
            "id": "BRO-1", "broker": "webull", "status": "SUBMITTED",
            "symbol": intent["symbol"], "side": intent["action"],
            "qty": 1, "notional": notional_usd,
        }

    monkeypatch.setattr("shared.broker_router.route_order", _fake_route_order)
    return captured


# ─── Case 1: legacy notional wins → brain_legacy ────────────────


@pytest.mark.asyncio
async def test_legacy_notional_wins_brain_legacy(monkeypatch):
    """`requested_notional_usd=25.00` (real +ve) with action=BUY →
    broker sees $25.00; `notional_source` == 'brain_legacy'."""
    await init_ledger(1000.0, 500.0)
    intent_id = f"micro-notional-test-{uuid.uuid4()}"
    intent = await _insert_intent(intent_id, "BUY", legacy=25.0)

    _wire_common_patches(monkeypatch)
    captured = _capture_broker(monkeypatch)

    result = await auto_router._route_one(intent)
    assert result["verdict"] == "executed"
    assert captured["notional"] == 25.0

    # FOLLOW-UP GAP audit-trail assertion: currently NOT persisted.
    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc.get("notional_source") == "brain_legacy", (
        "notional_source not persisted on intent doc (audit gap)"
    )


# ─── Case 2: v3 envelope wins when legacy missing → brain_v3 ────


@pytest.mark.asyncio
async def test_v3_envelope_notional_brain_v3(monkeypatch):
    """`execution.notional_usd=15.00`, legacy missing, action=SELL →
    broker sees $15.00; `notional_source` == 'brain_v3'."""
    await init_ledger(1000.0, 500.0)
    intent_id = f"micro-notional-test-{uuid.uuid4()}"
    intent = await _insert_intent(intent_id, "SELL", v3=15.0)

    _wire_common_patches(monkeypatch)
    captured = _capture_broker(monkeypatch)

    result = await auto_router._route_one(intent)
    assert result["verdict"] == "executed"
    assert captured["notional"] == 15.0

    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc.get("notional_source") == "brain_v3", (
        "notional_source not persisted on intent doc (audit gap)"
    )


# ─── Case 3: directional BUY with both None → micro_live_default ─


@pytest.mark.asyncio
async def test_micro_default_directional_buy(monkeypatch):
    """Both notional slots None + action=BUY → broker sees $5.00
    (MICRO_LIVE_DEFAULT_USD default); `notional_source` ==
    'micro_live_default'."""
    monkeypatch.delenv("MICRO_LIVE_DEFAULT_USD", raising=False)
    await init_ledger(1000.0, 500.0)
    intent_id = f"micro-notional-test-{uuid.uuid4()}"
    intent = await _insert_intent(intent_id, "BUY")

    _wire_common_patches(monkeypatch)
    captured = _capture_broker(monkeypatch)

    result = await auto_router._route_one(intent)
    assert result["verdict"] == "executed"
    assert captured["notional"] == 5.0, (
        f"BUY with no notional must default to $5.00 micro-live, "
        f"got ${captured.get('notional')}"
    )

    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc.get("notional_source") == "micro_live_default", (
        "notional_source not persisted on intent doc (audit gap) — "
        "operator wants this in the post-mortem trail"
    )


# ─── Case 4: directional SELL with both None → micro_live_default ─


@pytest.mark.asyncio
async def test_micro_default_directional_sell_zero_notional(monkeypatch):
    """Both notional slots set to 0.0 (treated as None) + action=SELL
    → broker sees $5.00; `notional_source` == 'micro_live_default'.
    Uses 0.0 (not None) to exercise the falsy-zero branch."""
    monkeypatch.delenv("MICRO_LIVE_DEFAULT_USD", raising=False)
    await init_ledger(1000.0, 500.0)
    intent_id = f"micro-notional-test-{uuid.uuid4()}"
    intent = await _insert_intent(intent_id, "SELL", legacy=0.0, v3=0.0)

    _wire_common_patches(monkeypatch)
    captured = _capture_broker(monkeypatch)

    result = await auto_router._route_one(intent)
    assert result["verdict"] == "executed"
    assert captured["notional"] == 5.0

    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc.get("notional_source") == "micro_live_default"


# ─── Case 5: HOLD → env_default (does NOT get $5 micro fallback) ─


@pytest.mark.asyncio
async def test_hold_action_falls_through_to_env_default(monkeypatch):
    """Non-directional (HOLD) with no notional MUST NOT get the
    micro-live $5 fallback — that fallback is directional-only.
    Should fall through to AUTO_ROUTER_NOTIONAL_USD (default $10);
    `notional_source` == 'env_default'."""
    monkeypatch.delenv("MICRO_LIVE_DEFAULT_USD", raising=False)
    await init_ledger(1000.0, 500.0)
    intent_id = f"micro-notional-test-{uuid.uuid4()}"
    intent = await _insert_intent(intent_id, "HOLD")

    _wire_common_patches(monkeypatch)
    captured = _capture_broker(monkeypatch)

    result = await auto_router._route_one(intent)
    assert result["verdict"] == "executed"
    # AUTO_ROUTER_NOTIONAL_USD is read at module-import time; use the
    # imported value in case the env overrode it.
    assert captured["notional"] == auto_router.AUTO_ROUTER_NOTIONAL_USD, (
        f"HOLD must not get micro $5 fallback; expected "
        f"${auto_router.AUTO_ROUTER_NOTIONAL_USD}, got ${captured.get('notional')}"
    )
    # Explicitly assert it is NOT the $5 micro-default so this test
    # catches a regression where HOLD falls into the directional branch.
    assert captured["notional"] != 5.0

    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc.get("notional_source") == "env_default"


# ─── Case 6: MICRO_LIVE_DEFAULT_USD env override ─────────────────


@pytest.mark.asyncio
async def test_env_override_micro_live_default_usd(monkeypatch):
    """`MICRO_LIVE_DEFAULT_USD=1.00` env override → the directional
    fallback is $1.00 instead of $5.00."""
    monkeypatch.setenv("MICRO_LIVE_DEFAULT_USD", "1.00")
    await init_ledger(1000.0, 500.0)
    intent_id = f"micro-notional-test-{uuid.uuid4()}"
    intent = await _insert_intent(intent_id, "BUY")

    _wire_common_patches(monkeypatch)
    captured = _capture_broker(monkeypatch)

    result = await auto_router._route_one(intent)
    assert result["verdict"] == "executed"
    assert captured["notional"] == 1.0, (
        f"MICRO_LIVE_DEFAULT_USD=1.00 override must produce $1.00 "
        f"notional, got ${captured.get('notional')}"
    )

    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc.get("notional_source") == "micro_live_default"
