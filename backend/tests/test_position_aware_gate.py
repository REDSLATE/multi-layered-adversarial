"""Regression tests for the `position_aware_intent_classification` gate.

Doctrine pin (2026-06-10, P2): finally wires the position-aware
classifier from `shared/position_model.py` into `_evaluate_gates`.
This was deferred on 2026-06-09 while live trading was active —
now landed with enforcement mode operator-controlled.

The gate compares the brain's claimed `position_evolution` against
the classifier's verdict using the LIVE broker position. On
disagreement:
  * audit_only mode (default) → records a misread row, gate passes
  * block mode                → records misread row + gate FAILS

These tests pin the contract across both modes and the corner cases
(missing position context, missing claim, lookup failure).
"""
from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared.execution import _evaluate_gates  # noqa: E402


def _intent(**overrides):
    base = {
        "intent_id": "i-1",
        "stack": "camaro",
        "action": "BUY",
        "symbol": "AAPL",
        "lane": "equity",
        "qty": 1.0,
        "may_execute": False,
        "requires_gate_pass": True,
        "holds_executor_seat": True,
        "executor_holder_at_post": "camaro",
        "snapshot": {"spread_bps": 5.0},
    }
    base.update(overrides)
    return base


def _patch_position_context(*, current_side: str, signed_qty: float):
    """Stub `position_context.get_position_context` so the gate sees
    a deterministic broker truth."""
    return patch(
        "shared.position_context.get_position_context",
        new=AsyncMock(return_value={
            "current_side": current_side,
            "signed_qty": signed_qty,
        }),
    )


def _patch_enforcement(active: bool):
    return patch(
        "routes.position_misread_admin.is_misread_enforcement_enabled",
        new=AsyncMock(return_value=active),
    )


def _patch_misread_insert():
    """Stub the misread insert so tests don't pollute the live db."""
    fake = AsyncMock(return_value=None)
    return patch.dict(
        # Patch the global `db` object's collection accessor used in
        # the gate. The simplest mock is to replace the collection
        # itself.
        {}, {},  # placeholder; real work in fixture below
    ), fake


@pytest.fixture
def _no_misread_writes(monkeypatch):
    """Replace the misread-insert path with an in-memory recorder so
    tests don't write rows to the live db."""
    from db import db as _db
    inserted: list[dict] = []
    fake_collection = AsyncMock()
    fake_collection.insert_one = AsyncMock(
        side_effect=lambda doc: inserted.append(doc)
    )
    real_getitem = _db.__class__.__getitem__

    def _getitem(self, name):
        if name == "shared_position_misreads":
            return fake_collection
        return real_getitem(self, name)

    monkeypatch.setattr(_db.__class__, "__getitem__", _getitem)
    yield inserted


# ── Happy path: agreement → pass ──────────────────────────────────


@pytest.mark.asyncio
async def test_buy_against_flat_with_open_claim_passes(_no_misread_writes):
    """Brain claims OPEN; broker says FLAT. classify_intent returns
    OPEN. They agree → gate passes, no misread recorded."""
    intent = _intent(position_evolution="open", current_side="flat")
    with ExitStack() as stack:
        stack.enter_context(_patch_position_context(
            current_side="flat", signed_qty=0.0,
        ))
        stack.enter_context(_patch_enforcement(False))
        res = await _evaluate_gates(intent, order_notional_usd=10.0)
    pa = next(g for g in res["gates"]
              if g["name"] == "position_aware_intent_classification")
    assert pa["passed"] is True
    assert "agrees" in pa["reason"]
    assert _no_misread_writes == [], "no misread should be recorded on agreement"


@pytest.mark.asyncio
async def test_buy_against_long_with_add_claim_passes(_no_misread_writes):
    """BUY against existing LONG = ADD; brain claims ADD; agreed."""
    intent = _intent(position_evolution="add", current_side="long")
    with ExitStack() as stack:
        stack.enter_context(_patch_position_context(
            current_side="long", signed_qty=10.0,
        ))
        stack.enter_context(_patch_enforcement(False))
        res = await _evaluate_gates(intent, order_notional_usd=10.0)
    pa = next(g for g in res["gates"]
              if g["name"] == "position_aware_intent_classification")
    assert pa["passed"] is True


# ── The AAPL pattern: brain thinks FLAT, broker says SHORT ────────


@pytest.mark.asyncio
async def test_aapl_pattern_buy_against_short_audit_only_passes(_no_misread_writes):
    """The 2026-06-09 incident shape:
    Brain reads FLAT, emits BUY (claiming OPEN). Broker actually
    holds SHORT — the BUY is a COVER. Under audit_only the gate
    PASSES but records a misread row."""
    intent = _intent(
        action="BUY",
        position_evolution="open",   # brain claims it's opening long
        current_side="flat",          # brain assumes flat
    )
    with ExitStack() as stack:
        stack.enter_context(_patch_position_context(
            current_side="short", signed_qty=-10.0,  # actually short
        ))
        stack.enter_context(_patch_enforcement(False))  # audit_only
        res = await _evaluate_gates(intent, order_notional_usd=10.0)
    pa = next(g for g in res["gates"]
              if g["name"] == "position_aware_intent_classification")
    assert pa["passed"] is True, "audit_only must NOT block the gate"
    assert "position_misread_observed (audit_only)" in pa["reason"]
    assert "shared_position_misreads" in pa["reason"]
    # Misread row was recorded
    assert len(_no_misread_writes) == 1
    rec = _no_misread_writes[0]
    assert rec["kind"] == "MISREAD_POSITION_SIDE"
    assert rec["symbol"] == "AAPL"
    assert rec["assumed_side"] == "flat"
    assert rec["actual_side"] == "short"
    assert rec["missed_short_profit"] is True


@pytest.mark.asyncio
async def test_aapl_pattern_buy_against_short_block_mode_FAILS(_no_misread_writes):
    """Under enforcement=block the gate FAILS hard. This is the
    structural safety net for the 06-09 AAPL pattern."""
    intent = _intent(
        action="BUY",
        position_evolution="open",
        current_side="flat",
    )
    with ExitStack() as stack:
        stack.enter_context(_patch_position_context(
            current_side="short", signed_qty=-10.0,
        ))
        stack.enter_context(_patch_enforcement(True))  # BLOCK mode
        res = await _evaluate_gates(intent, order_notional_usd=10.0)
    pa = next(g for g in res["gates"]
              if g["name"] == "position_aware_intent_classification")
    assert pa["passed"] is False, "block mode MUST fail the gate"
    assert "POSITION_MISREAD" in pa["reason"]
    assert "Enforcement is ACTIVE" in pa["reason"]
    assert res["verdict"] == "would_block"


# ── Symmetric inversion: brain thinks LONG, broker says SHORT ────


@pytest.mark.asyncio
async def test_sell_to_add_short_misclassified_as_close_audit(_no_misread_writes):
    """Brain thinks it's CLOSing a long, but it's actually SHORT and
    SELL on a short = ADD (growing exposure). Critical misclass."""
    intent = _intent(
        action="SELL",
        position_evolution="close",  # brain thinks it's closing a long
        current_side="long",
    )
    with ExitStack() as stack:
        stack.enter_context(_patch_position_context(
            current_side="short", signed_qty=-10.0,
        ))
        stack.enter_context(_patch_enforcement(False))
        res = await _evaluate_gates(intent, order_notional_usd=10.0)
    pa = next(g for g in res["gates"]
              if g["name"] == "position_aware_intent_classification")
    assert pa["passed"] is True  # audit_only
    # The misread detector trips on the assumed_side != actual_side axis.
    assert len(_no_misread_writes) == 1
    rec = _no_misread_writes[0]
    assert rec["assumed_side"] == "long"
    assert rec["actual_side"] == "short"


# ── Corner cases ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_passes_when_position_context_unavailable(_no_misread_writes):
    """If `get_position_context` raises, the gate passes with a
    diagnostic reason — we don't fail-closed on lookup errors."""
    intent = _intent(position_evolution="open")
    with ExitStack() as stack:
        stack.enter_context(patch(
            "shared.position_context.get_position_context",
            new=AsyncMock(side_effect=RuntimeError("broker offline")),
        ))
        stack.enter_context(_patch_enforcement(True))
        res = await _evaluate_gates(intent, order_notional_usd=10.0)
    pa = next(g for g in res["gates"]
              if g["name"] == "position_aware_intent_classification")
    assert pa["passed"] is True
    assert "position-aware check unavailable" in pa["reason"]


@pytest.mark.asyncio
async def test_skipped_for_non_routable_actions(_no_misread_writes):
    """HOLD is not routable — the gate must not run."""
    intent = _intent(action="HOLD")
    # Don't patch position_context — gate must not call it
    with _patch_enforcement(True):
        res = await _evaluate_gates(intent, order_notional_usd=10.0)
    names = [g["name"] for g in res["gates"]]
    assert "position_aware_intent_classification" not in names


@pytest.mark.asyncio
async def test_skipped_when_intended_qty_zero(_no_misread_writes):
    """qty=0 → can't classify anything — gate passes with skip note."""
    intent = _intent(qty=0.0, position_evolution="open")
    with ExitStack() as stack:
        stack.enter_context(_patch_position_context(
            current_side="short", signed_qty=-10.0,
        ))
        stack.enter_context(_patch_enforcement(True))
        res = await _evaluate_gates(intent, order_notional_usd=10.0)
    pa = next(g for g in res["gates"]
              if g["name"] == "position_aware_intent_classification")
    assert pa["passed"] is True
    assert "no inventory data" in pa["reason"]


@pytest.mark.asyncio
async def test_gate_appears_before_cap_evaluations(_no_misread_writes):
    """The position-aware gate must run BEFORE cap evaluations so caps
    aren't computed on a misclassified intent."""
    intent = _intent(position_evolution="open", current_side="flat")
    with ExitStack() as stack:
        stack.enter_context(_patch_position_context(
            current_side="flat", signed_qty=0.0,
        ))
        stack.enter_context(_patch_enforcement(False))
        res = await _evaluate_gates(intent, order_notional_usd=10.0)
    names = [g["name"] for g in res["gates"]]
    pa_idx = names.index("position_aware_intent_classification")
    # cap_per_order is always last in the cap section
    cap_idx = names.index("cap_per_order")
    assert pa_idx < cap_idx, (
        f"position_aware must precede cap_per_order; got order: {names}"
    )
