"""Integration tests for the capital-ledger wiring in `_route_one`.

Tests the reserve-before-broker-submit flow and the release paths
(broker terminal reject, position close). Uses in-memory monkey-
patched seat/risk/broker so we exercise the actual _route_one
control flow without touching the network.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, "/app/backend")


@pytest.fixture(autouse=True)
def _stub_entry_timing_gate(monkeypatch):
    """2026-08-01 Entry Timing Gate fails CLOSED without snapshot/
    bars, which these synthetic intents lack. Stub to allow — the
    gate has its own test file (test_entry_timing_gate.py)."""
    import shared.risk_sizer.entry_timing  # noqa: F401,WPS433
    monkeypatch.setattr(
        "shared.risk_sizer.entry_timing.check_buy_entry",
        AsyncMock(return_value={"allowed": True, "reason": "test_stub",
                                "decision": "BUY", "receipt": {}}),
    )

from db import db
from namespaces import CAPITAL_LEDGER, SHARED_INTENTS
from shared import auto_router
from shared.capital.ledger import (
    get_lane_headroom,
    init_ledger,
    release_capital,
    reserve_capital,
)


@pytest.fixture(autouse=True)
async def _clean_state():
    await db[CAPITAL_LEDGER].delete_many({})
    # Belt-and-braces cleanup: match both the `ledger-test-` prefix AND
    # any intent whose broker_order.id is the `"BRO-1"` fixture literal
    # from `_route_one` monkeypatches below. Some tests build the intent
    # doc directly (bypassing our `intent_id` naming convention), and
    # if any such doc reaches gate_state=submitted with broker_order.id
    # stamped, it leaks into `/api/intents` and shows up on the
    # operator dashboard as a "trade made today" (2026-07-08 incident).
    await db[SHARED_INTENTS].delete_many({
        "$or": [
            {"intent_id": {"$regex": "^ledger-test-"}},
            {"broker_order.id": "BRO-1"},
        ],
    })
    yield
    await db[CAPITAL_LEDGER].delete_many({})
    await db[SHARED_INTENTS].delete_many({
        "$or": [
            {"intent_id": {"$regex": "^ledger-test-"}},
            {"broker_order.id": "BRO-1"},
        ],
    })
    # Reset cached `shared.<mod>` attributes so downstream test files
    # that patch `sys.modules['shared.<mod>']` (via
    # `test_live_execution_path.py::_apply_patches`) see the patch.
    # Without this, `from shared import seat/risk/executions` inside
    # `_route_one` bypasses the sys.modules patch and hits the REAL
    # module — because Python resolves `from A import B` through the
    # cached `A.B` attribute when it exists.
    import shared as _shared_pkg
    for _attr in ("seat", "risk", "executions"):
        try:
            delattr(_shared_pkg, _attr)
        except AttributeError:
            pass


def _make_seat_ok(risk_mult=1.0, executor="camino"):
    """Fake seat decision that fires."""
    return SimpleNamespace(
        verdict="fire",
        reason="ok",
        risk_multiplier=risk_mult,
        strategist="camino",
        governor="camino",
        executor=executor,
        auditor="camino",
        angels={},
        intent_brain="camino",
        lane="equity",
    )


def _make_risk_ok(notional):
    return SimpleNamespace(
        ok=True,
        reason="ok",
        notional_usd=notional,
    )


@pytest.fixture(autouse=True)
def _timing_gate_open(monkeypatch):
    """Entry Timing Gate (2026-08-01) is fail-closed on missing timing
    data; these synthetic intents carry no snapshot/ingest_ts — stub
    it open like the other gates (ledger wiring is the subject)."""
    monkeypatch.setattr(
        "shared.risk_sizer.entry_timing.check_buy_entry",
        AsyncMock(return_value={"allowed": True, "reason": "entry_window_open",
                                "decision": "BUY", "receipt": {}}),
    )


async def _insert_intent(intent_id: str, lane: str, action: str, notional: float):
    """Insert a base intent doc the executor path expects to update."""
    doc = {
        "intent_id": intent_id,
        "symbol": "AAPL" if lane == "equity" else "BTC/USD",
        "action": action,
        "lane": lane,
        "stack": "camino",
        "stack_canonical": "camino",
        "requested_notional_usd": notional,
        "gate_state": "queued",
        # 2026-07-22 opportunity doctrine: authority window + action
        # tiers need a fresh ingest_ts and a conviction above `enter`.
        "ingest_ts": datetime.now(timezone.utc).isoformat(),
        "confidence": 0.6,
    }
    await db[SHARED_INTENTS].insert_one(doc)
    return doc


# ─────────── happy-path: live route reserves before broker submit ───────────


@pytest.mark.asyncio
async def test_live_micro_entry_reserves_capital_before_broker(monkeypatch):
    await init_ledger(1000.0, 500.0)
    intent_id = f"ledger-test-{uuid.uuid4()}"
    intent = await _insert_intent(intent_id, "equity", "BUY", 50.0)

    # Fake seat + risk paths — real modules but stubbed methods.
    import shared.seat as seat  # noqa: E402
    import shared.risk as risk  # noqa: E402
    import shared.executions as executions  # noqa: E402
    monkeypatch.setattr(seat, "decide", AsyncMock(return_value=_make_seat_ok()))
    monkeypatch.setattr(risk, "check", AsyncMock(return_value=_make_risk_ok(50.0)))
    monkeypatch.setattr(executions, "record", AsyncMock())
    # Master-switch preflight (added 2026-02-19) fails-closed when
    # the `mc_switch` Mongo doc is missing. Tests don't seed it, so
    # force-arm the switch here so the ledger gate can be reached.
    monkeypatch.setattr(
        "shared.auto_router._is_master_switch_armed",
        AsyncMock(return_value=True),
    )

    # Ladder sizing → live_micro route (BUY is entry).
    monkeypatch.setattr(
        "shared.sizing_gate.evaluate_sizing_with_ladder",
        AsyncMock(return_value=SimpleNamespace(
            final_usd=50.0, route="live_micro", stage="micro_live",
            binding_rail="ladder", ladder_cap_usd=100.0,
            execution_mode="ladder_live_micro",
        )),
    )

    # Bypass market_closed guard.
    monkeypatch.setattr("shared.market_hours.is_equity_rth", lambda: True)
    monkeypatch.setattr(
        "routes.equity_extended_hours_admin.get_equity_extended_hours_enabled",
        AsyncMock(return_value=False),
    )

    # Capture broker call
    captured: dict = {}

    async def _fake_route_order(intent, notional_usd, client_order_id):
        captured["notional"] = notional_usd
        # Assert reservation is in place BEFORE broker gets called.
        head = await get_lane_headroom("equity")
        captured["reserved_before_broker"] = head["reserved"]
        return {
            "id": "BRO-1", "broker": "webull", "status": "SUBMITTED",
            "symbol": intent["symbol"], "side": intent["action"],
            "qty": 1, "notional": notional_usd,
        }

    monkeypatch.setattr("shared.broker_router.route_order", _fake_route_order)

    result = await auto_router._route_one(intent)
    assert result["verdict"] == "executed"
    # The reservation held EXACTLY during broker submit.
    assert captured["reserved_before_broker"] == 50.0
    # After successful submit, the reservation is STILL held —
    # release comes on position close or terminal reject, not here.
    head = await get_lane_headroom("equity")
    assert head["reserved"] == 50.0


@pytest.mark.asyncio
async def test_observe_route_does_not_reserve(monkeypatch):
    """Route=observe → SKIP the ledger reserve entirely."""
    await init_ledger(1000.0, 500.0)
    intent_id = f"ledger-test-{uuid.uuid4()}"
    intent = await _insert_intent(intent_id, "equity", "BUY", 50.0)

    import shared.seat as seat  # noqa: E402
    import shared.risk as risk  # noqa: E402
    import shared.executions as executions  # noqa: E402
    monkeypatch.setattr(seat, "decide", AsyncMock(return_value=_make_seat_ok()))
    monkeypatch.setattr(risk, "check", AsyncMock(return_value=_make_risk_ok(50.0)))
    monkeypatch.setattr(executions, "record", AsyncMock())
    # Master-switch preflight (added 2026-02-19) fails-closed when
    # the `mc_switch` Mongo doc is missing. Tests don't seed it, so
    # force-arm the switch here so the ledger gate can be reached.
    monkeypatch.setattr(
        "shared.auto_router._is_master_switch_armed",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "shared.sizing_gate.evaluate_sizing_with_ladder",
        AsyncMock(return_value=SimpleNamespace(
            final_usd=50.0, route="observe", stage="observation_only",
            binding_rail="ladder_observation", ladder_cap_usd=0.0,
            execution_mode="observation_only",
        )),
    )
    monkeypatch.setattr("shared.market_hours.is_equity_rth", lambda: True)
    monkeypatch.setattr(
        "routes.equity_extended_hours_admin.get_equity_extended_hours_enabled",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "shared.broker_router.route_order",
        AsyncMock(return_value={
            "id": "BRO-1", "broker": "webull", "status": "SUBMITTED",
        }),
    )

    await auto_router._route_one(intent)
    head = await get_lane_headroom("equity")
    assert head["reserved"] == 0.0, (
        "observe route must not consume ledger headroom"
    )


@pytest.mark.asyncio
async def test_sell_action_does_not_reserve(monkeypatch):
    """SELL is an EXIT action — releases via position_close, does NOT reserve."""
    await init_ledger(1000.0, 500.0)
    intent_id = f"ledger-test-{uuid.uuid4()}"
    intent = await _insert_intent(intent_id, "equity", "SELL", 50.0)

    import shared.seat as seat  # noqa: E402
    import shared.risk as risk  # noqa: E402
    import shared.executions as executions  # noqa: E402
    monkeypatch.setattr(seat, "decide", AsyncMock(return_value=_make_seat_ok()))
    monkeypatch.setattr(risk, "check", AsyncMock(return_value=_make_risk_ok(50.0)))
    monkeypatch.setattr(executions, "record", AsyncMock())
    # Master-switch preflight (added 2026-02-19) fails-closed when
    # the `mc_switch` Mongo doc is missing. Tests don't seed it, so
    # force-arm the switch here so the ledger gate can be reached.
    monkeypatch.setattr(
        "shared.auto_router._is_master_switch_armed",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "shared.sizing_gate.evaluate_sizing_with_ladder",
        AsyncMock(return_value=SimpleNamespace(
            final_usd=50.0, route="live_micro", stage="micro_live",
            binding_rail="ladder", ladder_cap_usd=100.0,
            execution_mode="ladder_live_micro",
        )),
    )
    monkeypatch.setattr("shared.market_hours.is_equity_rth", lambda: True)
    monkeypatch.setattr(
        "routes.equity_extended_hours_admin.get_equity_extended_hours_enabled",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "shared.broker_router.route_order",
        AsyncMock(return_value={
            "id": "BRO-1", "broker": "webull", "status": "SUBMITTED",
        }),
    )

    await auto_router._route_one(intent)
    head = await get_lane_headroom("equity")
    assert head["reserved"] == 0.0, (
        "SELL is an exit — must not reserve capital"
    )


# ─────────── cap-exceeded rejection ───────────


@pytest.mark.asyncio
async def test_cap_exceeded_blocks_before_broker(monkeypatch):
    """When ledger reserve fails (cap exceeded), the broker call
    MUST NOT be reached — the intent gets stamped REJECTED_CAP_EXCEEDED."""
    await init_ledger(100.0, 500.0)   # tight equity cap
    # Fill the cap first.
    await reserve_capital("equity", 90.0, "prior-intent")

    intent_id = f"ledger-test-{uuid.uuid4()}"
    intent = await _insert_intent(intent_id, "equity", "BUY", 50.0)

    import shared.seat as seat  # noqa: E402
    import shared.risk as risk  # noqa: E402
    import shared.executions as executions  # noqa: E402
    monkeypatch.setattr(seat, "decide", AsyncMock(return_value=_make_seat_ok()))
    monkeypatch.setattr(risk, "check", AsyncMock(return_value=_make_risk_ok(50.0)))
    monkeypatch.setattr(executions, "record", AsyncMock())
    # Master-switch preflight (added 2026-02-19) fails-closed when
    # the `mc_switch` Mongo doc is missing. Tests don't seed it, so
    # force-arm the switch here so the ledger gate can be reached.
    monkeypatch.setattr(
        "shared.auto_router._is_master_switch_armed",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "shared.sizing_gate.evaluate_sizing_with_ladder",
        AsyncMock(return_value=SimpleNamespace(
            final_usd=50.0, route="live_micro", stage="micro_live",
            binding_rail="ladder", ladder_cap_usd=100.0,
            execution_mode="ladder_live_micro",
        )),
    )
    # Bypass market_closed guard — ledger gate now runs AFTER it.
    monkeypatch.setattr("shared.market_hours.is_equity_rth", lambda: True)
    monkeypatch.setattr(
        "routes.equity_extended_hours_admin.get_equity_extended_hours_enabled",
        AsyncMock(return_value=False),
    )
    broker_mock = AsyncMock()
    monkeypatch.setattr("shared.broker_router.route_order", broker_mock)

    result = await auto_router._route_one(intent)

    assert result["verdict"] == "blocked"
    assert result["reason"] == "REJECTED_CAP_EXCEEDED"
    # BROKER MUST NEVER HAVE BEEN CALLED.
    assert broker_mock.await_count == 0
    # Intent stamped correctly.
    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc["gate_state"] == "blocked"
    assert doc["broker_reason"] == "REJECTED_CAP_EXCEEDED"
    # Ledger reserved unchanged (only the prior $90 is held).
    head = await get_lane_headroom("equity")
    assert head["reserved"] == 90.0


# ─────────── position close releases ───────────


@pytest.mark.asyncio
async def test_position_close_releases_ledger(monkeypatch):
    """Closing a live position triggers `release_capital` for the
    entry intent's reservation. Uses `opened_notional_usd` as the
    release amount."""
    from shared import live_positions
    from namespaces import SHARED_LIVE_POSITIONS

    await init_ledger(1000.0, 500.0)
    entry_intent_id = f"ledger-test-entry-{uuid.uuid4()}"
    await reserve_capital("equity", 75.0, entry_intent_id)

    # Insert a live-position doc in `managing` state.
    position_id = f"pos-{uuid.uuid4()}"
    await db[SHARED_LIVE_POSITIONS].insert_one({
        "position_id": position_id,
        "state": "managing",
        "intent_id": entry_intent_id,
        "lane": "equity",
        "stack": "camino",
        "symbol": "AAPL",
        "action": "BUY",
        "direction": "long",
        "opened_at": "2026-07-08T00:00:00+00:00",
        "opened_notional_usd": 75.0,
        "current_notional_usd": 78.0,
        "fills": [],
        "transitions": [],
    })

    head_before = await get_lane_headroom("equity")
    assert head_before["reserved"] == 75.0

    # Trigger close.
    try:
        await live_positions.close(
            position_id=position_id,
            actor="test",
            pnl_usd=3.0,
            pnl_pct=0.04,
            outcome_label="win",
            note="test-close",
            broker_order_id="BRO-EXIT-1",
        )
    finally:
        await db[SHARED_LIVE_POSITIONS].delete_one({"position_id": position_id})

    head_after = await get_lane_headroom("equity")
    assert head_after["reserved"] == 0.0, (
        f"position close must release ledger reservation, "
        f"reserved={head_after['reserved']}"
    )
