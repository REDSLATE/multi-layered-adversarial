"""Micro-notional fallback tests for `shared.auto_router._route_one`.

Locks in the 2026-07-09 REVISED operator directive
(`assign_micro_notional` rule):

    if execution.action in {BUY, SELL} and execution.notional_usd is None:
        if doctrine.failed_checks:           # ANY failed check
            execution.notional_usd = 1.00
            execution.notional_source = "micro_probe_failed_quality"
        else:                                # doctrine clean
            execution.notional_usd = 5.00
            execution.notional_source = "micro_default"

Notional resolution precedence inside `_route_one`:

    1. `intent.requested_notional_usd`  → notional_source = "brain_legacy"
    2. `intent.execution.notional_usd`  → notional_source = "brain_v3"
    3. directional (BUY/SELL) w/ both None:
       a. doctrine has ANY failed_checks → $1 (env MICRO_PROBE_FAILED_QUALITY_USD)
          → notional_source = "micro_probe_failed_quality"
       b. doctrine clean → $5 (env MICRO_LIVE_DEFAULT_USD)
          → notional_source = "micro_default"
    4. anything else (HOLD, etc.)       → AUTO_ROUTER_NOTIONAL_USD ($10)
       → notional_source = "env_default"

These tests reuse the same monkeypatch pattern as
`test_capital_ledger_wiring.py` — the real `_route_one` control flow
runs, but seat/risk/broker are stubbed. We inspect the notional the
mocked broker ends up seeing to determine what got resolved.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
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
        # Fresh ingest_ts — the 2026-07-22 authority-window gate blocks
        # stale/absent timestamps before the notional stage under test.
        "ingest_ts": datetime.now(timezone.utc).isoformat(),
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
    # These tests predate the 2026-07-22 tier doctrine — pin tiers OFF
    # in the ExecutionPolicySnapshot so the tier gate doesn't override
    # the legacy/v3 notional resolution under test.
    from shared.hotpath import policy_snapshot
    from shared.opportunity.policy import _merge
    _pol = _merge({})
    _pol["tiers_enabled"] = False
    policy_snapshot._dirty = False  # noqa: SLF001
    policy_snapshot.apply_local(opportunity_policy=_pol)
    # 2026-07-09 sys.modules leak fix: touch these modules FIRST so
    # pytest's monkeypatch resolver and `_route_one`'s runtime
    # `from ... import ...` calls both see the same module object.
    # After `test_live_execution_path.py` runs its patch.dict cycle,
    # sys.modules can end up in a state where two different module
    # objects for these names are referenced by different code
    # paths — importing here re-anchors them.
    import shared.market_hours  # noqa: F401,WPS433
    import shared.seat  # noqa: F401,WPS433
    import shared.risk  # noqa: F401,WPS433
    import shared.executions  # noqa: F401,WPS433
    import shared.sizing_gate  # noqa: F401,WPS433
    import shared.broker_router  # noqa: F401,WPS433
    import routes.equity_extended_hours_admin  # noqa: F401,WPS433

    # Neutralize the Webull $5 equity floor size-up (post-dates these
    # tests) — this file pins notional RESOLUTION, not broker floors.
    monkeypatch.setattr(
        "shared.broker.webull_caps.webull_notional_band",
        lambda _q=None: (0.0, 100000.0, "test"),
    )

    import shared.seat as seat
    import shared.risk as risk
    import shared.executions as executions
    monkeypatch.setattr(seat, "decide", AsyncMock(return_value=_seat_fire()))
    monkeypatch.setattr(risk, "check", _risk_pass_through())
    monkeypatch.setattr(executions, "record", AsyncMock())
    # Master-switch preflight (added 2026-02-19) gates `_route_one`
    # on the `mc_switch` Mongo doc. In tests the doc doesn't exist,
    # so the switch fails-closed (DISARMED). Force-arm here — these
    # tests are focused on notional resolution, not the switch.
    monkeypatch.setattr(
        "shared.auto_router._is_master_switch_armed",
        AsyncMock(return_value=True),
    )
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
    # 2026-08-01 Entry Timing Gate — fails CLOSED without snapshot/
    # bars, which these synthetic intents lack. Stub to allow (gate
    # has its own dedicated test file: test_entry_timing_gate.py).
    import shared.risk_sizer.entry_timing  # noqa: F401,WPS433
    monkeypatch.setattr(
        "shared.risk_sizer.entry_timing.check_buy_entry",
        AsyncMock(return_value={"allowed": True, "reason": "test_stub",
                                "decision": "BUY", "receipt": {}}),
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
    """Both notional slots None + action=BUY + doctrine CLEAN
    (no failed_checks) → broker sees $5.00 (MICRO_LIVE_DEFAULT_USD
    default); `notional_source` == 'micro_default'."""
    monkeypatch.delenv("MICRO_LIVE_DEFAULT_USD", raising=False)
    await init_ledger(1000.0, 500.0)
    intent_id = f"micro-notional-test-{uuid.uuid4()}"
    intent = await _insert_intent(intent_id, "BUY")

    _wire_common_patches(monkeypatch)
    captured = _capture_broker(monkeypatch)

    result = await auto_router._route_one(intent)
    assert result["verdict"] == "executed"
    assert captured["notional"] == 5.0, (
        f"BUY with no notional + clean doctrine must default to "
        f"$5.00 micro-default, got ${captured.get('notional')}"
    )

    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc.get("notional_source") == "micro_default", (
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
    assert doc.get("notional_source") == "micro_default"


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
    assert doc.get("notional_source") == "micro_default"


# ─── Case 7: directional BUY + failed_checks → $1 quality probe ───


@pytest.mark.asyncio
async def test_failed_quality_directional_ships_1_dollar_probe(monkeypatch):
    """Directional (BUY/SELL) intent with no notional AND doctrine
    reports ANY failed_checks → broker sees $1.00; `notional_source`
    == 'micro_probe_failed_quality'.

    Locks in the 2026-07-09 revised rule:

        Direction exists, but quality is weak → probe only.

    Any failed check triggers the probe (not just the specific
    marginal-setup triple). The operator's original rule intentionally
    prioritized "flow the trade at a token size" over "reject on any
    quality flag" so a wider set of doctrine outcomes still touch
    the market."""
    monkeypatch.delenv("MICRO_LIVE_DEFAULT_USD", raising=False)
    monkeypatch.delenv("MICRO_PROBE_FAILED_QUALITY_USD", raising=False)
    await init_ledger(1000.0, 500.0)
    intent_id = f"micro-notional-test-{uuid.uuid4()}"
    intent = await _insert_intent(intent_id, "BUY")

    # Stamp the doctrine packet in Mongo so `_route_one` reads it back.
    packet = {
        "seats": {
            "execution_judge": {
                "execution_ready": False,
                "failed_checks": ["liquidity_ok", "quality_ok", "score_ok"],
            },
        },
    }
    await db[SHARED_INTENTS].update_one(
        {"intent_id": intent_id},
        {"$set": {"doctrine_packet": packet}},
    )
    intent["doctrine_packet"] = packet

    _wire_common_patches(monkeypatch)
    captured = _capture_broker(monkeypatch)

    result = await auto_router._route_one(intent)
    assert result["verdict"] == "executed"
    assert captured["notional"] == 1.0, (
        f"failed_checks present → $1 probe, got ${captured.get('notional')}"
    )
    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc.get("notional_source") == "micro_probe_failed_quality"


# ─── Case 8: single failed check is enough → $1 probe ─────────────


@pytest.mark.asyncio
async def test_single_failed_check_still_probes(monkeypatch):
    """Even ONE failed check (not the marginal triple) is enough to
    downshift to a $1 probe — the rule is 'ANY failed_checks', not
    'specific failed_checks pattern'."""
    monkeypatch.delenv("MICRO_LIVE_DEFAULT_USD", raising=False)
    monkeypatch.delenv("MICRO_PROBE_FAILED_QUALITY_USD", raising=False)
    await init_ledger(1000.0, 500.0)
    intent_id = f"micro-notional-test-{uuid.uuid4()}"
    intent = await _insert_intent(intent_id, "SELL")

    packet = {
        "seats": {
            "execution_judge": {
                "failed_checks": ["spread_ok"],
            },
        },
    }
    await db[SHARED_INTENTS].update_one(
        {"intent_id": intent_id},
        {"$set": {"doctrine_packet": packet}},
    )
    intent["doctrine_packet"] = packet

    _wire_common_patches(monkeypatch)
    captured = _capture_broker(monkeypatch)

    result = await auto_router._route_one(intent)
    assert result["verdict"] == "executed"
    assert captured["notional"] == 1.0
    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc.get("notional_source") == "micro_probe_failed_quality"


# ─── Case 9: brain-sized intent is NOT overridden by failed_checks ─


@pytest.mark.asyncio
async def test_brain_sized_intent_survives_failed_checks(monkeypatch):
    """If the brain already sized the intent (legacy or v3), the
    `assign_micro_notional` rule leaves it alone — even when doctrine
    flags failed checks. The probe fallbacks are a NOTIONAL RESOLUTION
    step (default when brain didn't size), not a doctrine downshift."""
    await init_ledger(1000.0, 500.0)
    intent_id = f"micro-notional-test-{uuid.uuid4()}"
    intent = await _insert_intent(intent_id, "BUY", legacy=42.0)

    packet = {
        "seats": {
            "execution_judge": {
                "failed_checks": ["liquidity_ok", "quality_ok", "score_ok"],
            },
        },
    }
    await db[SHARED_INTENTS].update_one(
        {"intent_id": intent_id},
        {"$set": {"doctrine_packet": packet}},
    )
    intent["doctrine_packet"] = packet

    _wire_common_patches(monkeypatch)
    captured = _capture_broker(monkeypatch)

    result = await auto_router._route_one(intent)
    assert result["verdict"] == "executed"
    assert captured["notional"] == 42.0, (
        "brain-sized intent must survive intact — probes only apply "
        "when notional_usd is null"
    )
    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc.get("notional_source") == "brain_legacy"
