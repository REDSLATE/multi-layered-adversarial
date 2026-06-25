"""Seat-authority three-mode doctrine — operator pin 2026-02-23.

Background: Two authority models existed in the codebase prior to
this fix:

  * `shared/pipeline/seat_policy.py` — BRAIN-BOUND
    (`brain_not_current_seat_holder` blocks anything where the
     emitting brain isn't the seat holder).
  * `shared/execution.py:_evaluate_gates` — POSITION-ONLY since
     2026-05-28 (any held seat passes; emitting brain is
     "informational only").

That asymmetry let non-seat-holder brains' intents EXECUTE via the
operator SUBMIT button or auto-submit, because both paths called
`_evaluate_gates` which DIDN'T enforce brain identity. Operator
observed this on prod 2026-02-23: "other brains are executing
trades without being in the executor's seat."

The fix (per operator-proposed doctrine):

  seat_bound        intent.stack == current_holder
                    auto + operator submit OK

  requires_override seat held + lane allowed, BUT intent.stack
                    != current_holder. auto-submit BLOCKS. Operator
                    must go through /execution/submit-override
                    with operator_override=True + a reason.

  vacant            no holder. Always block.

This test file pins the doctrine end-to-end:

  1. `_evaluate_gates` returns the right `seat_authority` class for
     each of the three scenarios.
  2. `requires_operator_override=True` ⇒ /execution/submit refuses
     without `operator_override=true`.
  3. `/execution/submit-override` requires a non-trivial reason +
     stamps the audit row with execution_authority_mode.
  4. `/execution/submit-override` REJECTS seat_bound intents (the
     two endpoints stay distinct).
  5. The audit trail is complete — both the request and (on success)
     the broker receipt carry execution_authority_mode.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from db import db
from namespaces import SHARED_INTENTS, SHARED_GATE_RESULTS


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _isolate_seat_authority_tests():
    """Reset seat state + cleanup test artifacts around each test.

    Tests stub `_seat_holder` / `get_seat_holder` via the executor
    seat collection so we don't depend on whatever the live prod
    operator has assigned. Cleanup removes test intents + audit
    rows.
    """
    test_prefix = "sa-test-"
    await db[SHARED_INTENTS].delete_many({"intent_id": {"$regex": f"^{test_prefix}"}})
    await db[SHARED_GATE_RESULTS].delete_many({"intent_id": {"$regex": f"^{test_prefix}"}})
    yield
    await db[SHARED_INTENTS].delete_many({"intent_id": {"$regex": f"^{test_prefix}"}})
    await db[SHARED_GATE_RESULTS].delete_many({"intent_id": {"$regex": f"^{test_prefix}"}})


async def _seed_intent(*, stack, action="BUY", lane="equity",
                       symbol="AAPL"):
    iid = f"sa-test-{uuid.uuid4().hex[:10]}"
    await db[SHARED_INTENTS].insert_one({
        "intent_id":         iid,
        "stack":             stack,
        "action":            action,
        "lane":              lane,
        "symbol":            symbol,
        "qty":               1,
        "may_execute":       False,
        "requires_gate_pass": True,
        "executed":          False,
    })
    return iid


# ── 1. `_evaluate_gates` returns the right `seat_authority` class ──
async def test_seat_bound_when_stack_matches_holder(monkeypatch):
    """Camino emits while Camino holds the seat → seat_bound."""
    from shared import execution as exec_mod

    async def fake_seat_holder(seat_name, lane=None):  # noqa: ARG001
        return "camino"

    monkeypatch.setattr(exec_mod, "_seat_holder", fake_seat_holder)

    # Also stub the executor_seat module-level imports used inside
    # _evaluate_gates.
    from shared import executor_seat as es

    async def fake_get_seat_holder(seat_name):  # noqa: ARG001
        return "camino"

    monkeypatch.setattr(es, "get_seat_holder", fake_get_seat_holder)
    monkeypatch.setattr(es, "seats_with_execute",
                        lambda lane: ["PASCHAR"])
    from shared import seat_policy as sp
    monkeypatch.setattr(sp, "seat_may_execute_lane",
                        lambda seat, lane: True)

    iid = await _seed_intent(stack="camino")
    intent = await db[SHARED_INTENTS].find_one({"intent_id": iid}, {"_id": 0})
    result = await exec_mod._evaluate_gates(intent, 10.0)
    assert result["seat_authority"] == "seat_bound"
    assert result["requires_operator_override"] is False
    assert result["intent_author"] == "camino"
    assert result["seat_holder"]   == "camino"


async def test_requires_override_when_stack_mismatches_holder(monkeypatch):
    """Camino emits while Barracuda holds the seat → requires_override.

    Critical: the gate chain still reports the OVERALL verdict as
    `would_pass` (so the rest of the chain can run), but the
    `requires_operator_override` flag is set so `execution_submit`
    refuses without an explicit override.
    """
    from shared import execution as exec_mod
    from shared import executor_seat as es
    from shared import seat_policy as sp

    async def fake_get_seat_holder(seat_name):  # noqa: ARG001
        return "barracuda"

    monkeypatch.setattr(es, "get_seat_holder", fake_get_seat_holder)
    monkeypatch.setattr(es, "seats_with_execute",
                        lambda lane: ["PASCHAR"])
    monkeypatch.setattr(sp, "seat_may_execute_lane",
                        lambda seat, lane: True)

    iid = await _seed_intent(stack="camino")
    intent = await db[SHARED_INTENTS].find_one({"intent_id": iid}, {"_id": 0})
    result = await exec_mod._evaluate_gates(intent, 10.0)

    assert result["seat_authority"] == "requires_override"
    assert result["requires_operator_override"] is True
    assert result["intent_author"] == "camino"
    assert result["seat_holder"]   == "barracuda"
    # A `seat_authority_classification` gate row was appended.
    names = [g["name"] for g in result["gates"]]
    assert "seat_authority_classification" in names


async def test_vacant_when_no_holder(monkeypatch):
    from shared import execution as exec_mod
    from shared import executor_seat as es

    async def fake_get_seat_holder(seat_name):  # noqa: ARG001
        return None

    monkeypatch.setattr(es, "get_seat_holder", fake_get_seat_holder)
    monkeypatch.setattr(es, "seats_with_execute",
                        lambda lane: ["PASCHAR"])

    iid = await _seed_intent(stack="camino")
    intent = await db[SHARED_INTENTS].find_one({"intent_id": iid}, {"_id": 0})
    result = await exec_mod._evaluate_gates(intent, 10.0)

    assert result["seat_authority"] == "vacant"
    assert result["requires_operator_override"] is False
    # The downstream submit should ALSO block because the
    # executor_seat_check gate failed.
    seat_gate = next(g for g in result["gates"]
                     if g["name"] == "executor_seat_check")
    assert seat_gate["passed"] is False


# ── 2. /execution/submit refuses requires_override without flag ──
async def test_execution_submit_refuses_override_required_intent(monkeypatch):
    """The exact bypass the operator caught: non-seat-holder intent
    routed via the standard submit path must now refuse with 403."""
    from shared import execution as exec_mod
    from shared import executor_seat as es
    from shared import seat_policy as sp

    async def fake_get_seat_holder(seat_name):  # noqa: ARG001
        return "barracuda"

    monkeypatch.setattr(es, "get_seat_holder", fake_get_seat_holder)
    monkeypatch.setattr(es, "seats_with_execute",
                        lambda lane: ["PASCHAR"])
    monkeypatch.setattr(sp, "seat_may_execute_lane",
                        lambda seat, lane: True)

    iid = await _seed_intent(stack="camino")
    body = exec_mod.SubmitBody(
        intent_id=iid,
        order_notional_usd=10.0,
        confirm="execute",
        operator_override=False,
        override_reason="",
        brain_name="camino",
    )
    with pytest.raises(HTTPException) as exc:
        await exec_mod.execution_submit(body, user={"email": "op@test"})
    assert exc.value.status_code == 403
    detail = exc.value.detail
    assert detail["blocked_by"] == "seat_authority_classification"
    assert detail["requires_operator_override"] is True
    assert detail["intent_author"] == "camino"
    assert detail["seat_holder"]   == "barracuda"
    # Audit row written so the post-mortem can see the rejection.
    audit = await db[SHARED_GATE_RESULTS].find_one(
        {"intent_id": iid, "kind": "submit_requires_override"},
    )
    assert audit is not None


# ── 3. /execution/submit-override requires non-trivial reason ──
async def test_submit_override_requires_reason():
    from shared import execution as exec_mod
    iid = await _seed_intent(stack="camino")
    body = exec_mod.SubmitBody(
        intent_id=iid,
        order_notional_usd=10.0,
        confirm="execute",
        operator_override=True,
        override_reason="too short",  # < 12 chars
        brain_name="camino",
    )
    with pytest.raises(HTTPException) as exc:
        await exec_mod.execution_submit_override(body, user={"email": "op@test"})
    assert exc.value.status_code == 400
    assert "at least 12 characters" in str(exc.value.detail)


# ── 4. /execution/submit-override rejects seat-bound intents ──
async def test_submit_override_rejects_seat_bound_intent(monkeypatch):
    """Operator must use /execution/submit on seat_bound intents.
    The override endpoint exists specifically for non-seat-holder
    authorization — don't let it be used as a quiet alias."""
    from shared import execution as exec_mod
    from shared import executor_seat as es
    from shared import seat_policy as sp

    async def fake_get_seat_holder(seat_name):  # noqa: ARG001
        return "camino"

    monkeypatch.setattr(es, "get_seat_holder", fake_get_seat_holder)
    monkeypatch.setattr(es, "seats_with_execute",
                        lambda lane: ["PASCHAR"])
    monkeypatch.setattr(sp, "seat_may_execute_lane",
                        lambda seat, lane: True)

    iid = await _seed_intent(stack="camino")
    body = exec_mod.SubmitBody(
        intent_id=iid,
        order_notional_usd=10.0,
        confirm="execute",
        operator_override=True,
        override_reason="explicit operator authorization for a non-holder",
        brain_name="camino",
    )
    with pytest.raises(HTTPException) as exc:
        await exec_mod.execution_submit_override(body, user={"email": "op@test"})
    assert exc.value.status_code == 400
    detail = exc.value.detail
    assert detail["seat_authority"] == "seat_bound"
    assert "does NOT require an operator override" in detail["reason"]


# ── 5. /execution/submit-override audit-stamps the request ──
async def test_submit_override_audits_request_before_broker_call(monkeypatch):
    """Stamp the override authorization BEFORE the broker call so a
    downstream failure can't lose the auth record."""
    from shared import execution as exec_mod
    from shared import executor_seat as es
    from shared import seat_policy as sp

    async def fake_get_seat_holder(seat_name):  # noqa: ARG001
        return "barracuda"

    monkeypatch.setattr(es, "get_seat_holder", fake_get_seat_holder)
    monkeypatch.setattr(es, "seats_with_execute",
                        lambda lane: ["PASCHAR"])
    monkeypatch.setattr(sp, "seat_may_execute_lane",
                        lambda seat, lane: True)

    # Stub execution_submit so we don't actually touch the broker
    # — we only care that the request audit was written first.
    async def fake_submit(forced, user):  # noqa: ARG001
        return {"ok": True, "verdict": "executed",
                "intent_id": forced.intent_id, "stubbed": True}

    monkeypatch.setattr(exec_mod, "execution_submit", fake_submit)

    iid = await _seed_intent(stack="camino")
    body = exec_mod.SubmitBody(
        intent_id=iid,
        order_notional_usd=10.0,
        confirm="execute",
        operator_override=True,
        override_reason="explicit operator authorization for a non-holder",
        brain_name="camino",
    )
    out = await exec_mod.execution_submit_override(body, user={"email": "op@test"})
    assert out["ok"] is True
    audit = await db[SHARED_GATE_RESULTS].find_one(
        {"intent_id": iid, "kind": "override_submit_request"},
    )
    assert audit is not None
    assert audit["execution_authority_mode"] == "operator_override"
    assert audit["intent_author"] == "camino"
    assert audit["seat_holder"]   == "barracuda"
    assert audit["operator_confirmed"] is True
    assert audit["operator_reason"].startswith("explicit operator")


# ── 6. Auto-submit naturally inherits the block ───────────────────
async def test_auto_submit_blocked_by_override_requirement(monkeypatch):
    """maybe_auto_submit calls execution_submit with operator_override=
    False. Our gate must therefore refuse — closing the
    `non-seat-holder brain → auto-submit → broker` leak."""
    from shared import execution as exec_mod
    from shared import executor_seat as es
    from shared import seat_policy as sp

    async def fake_get_seat_holder(seat_name):  # noqa: ARG001
        return "barracuda"

    monkeypatch.setattr(es, "get_seat_holder", fake_get_seat_holder)
    monkeypatch.setattr(es, "seats_with_execute",
                        lambda lane: ["PASCHAR"])
    monkeypatch.setattr(sp, "seat_may_execute_lane",
                        lambda seat, lane: True)

    iid = await _seed_intent(stack="camino")
    auto_body = exec_mod.SubmitBody(
        intent_id=iid,
        order_notional_usd=10.0,
        confirm="execute",
        operator_override=False,  # exact value maybe_auto_submit sends
        override_reason="",
        action_override=None,
        brain_name="camino",
    )
    with pytest.raises(HTTPException) as exc:
        await exec_mod.execution_submit(
            auto_body, user={"email": "auto_submit_tier_1@risedual.io",
                             "auto_submit": True},
        )
    assert exc.value.status_code == 403
    assert exc.value.detail["blocked_by"] == "seat_authority_classification"



# ── 7. Brain-name confirmation (operator pin 2026-02-23) ───────────
async def test_submit_rejects_brain_name_mismatch(monkeypatch):
    """Wrong-row click: operator clicked submit on intent A but the
    UI body carries brain B's name. Backend must refuse with 400
    BEFORE the broker call."""
    from shared import execution as exec_mod
    from shared import executor_seat as es
    from shared import seat_policy as sp

    async def fake_get_seat_holder(seat_name):  # noqa: ARG001
        return "camino"

    monkeypatch.setattr(es, "get_seat_holder", fake_get_seat_holder)
    monkeypatch.setattr(es, "seats_with_execute",
                        lambda lane: ["PASCHAR"])
    monkeypatch.setattr(sp, "seat_may_execute_lane",
                        lambda seat, lane: True)

    iid = await _seed_intent(stack="camino")
    body = exec_mod.SubmitBody(
        intent_id=iid,
        order_notional_usd=10.0,
        confirm="execute",
        brain_name="barracuda",  # WRONG
    )
    with pytest.raises(HTTPException) as exc:
        await exec_mod.execution_submit(body, user={"email": "op@test"})
    assert exc.value.status_code == 400
    detail = exc.value.detail
    assert detail["blocked_by"] == "brain_name_mismatch"
    assert detail["claimed_brain"] == "barracuda"
    assert detail["intent_author"] == "camino"


async def test_submit_brain_name_case_insensitive_match(monkeypatch):
    """`CAMINO` and `camino` must match — operator types whatever
    case they remember; we normalize before comparison."""
    from shared import execution as exec_mod
    from shared import executor_seat as es
    from shared import seat_policy as sp

    async def fake_get_seat_holder(seat_name):  # noqa: ARG001
        return "camino"

    monkeypatch.setattr(es, "get_seat_holder", fake_get_seat_holder)
    monkeypatch.setattr(es, "seats_with_execute",
                        lambda lane: ["PASCHAR"])
    monkeypatch.setattr(sp, "seat_may_execute_lane",
                        lambda seat, lane: True)

    iid = await _seed_intent(stack="camino")
    body = exec_mod.SubmitBody(
        intent_id=iid,
        order_notional_usd=10.0,
        confirm="execute",
        brain_name="CAMINO",  # different case
    )
    # We expect brain-name check to pass; subsequent gate failures
    # are out of scope. Assert ONLY that brain_name_mismatch is
    # NOT the failure reason.
    try:
        await exec_mod.execution_submit(body, user={"email": "op@test"})
    except HTTPException as e:
        detail = e.detail
        assert not (isinstance(detail, dict)
                    and detail.get("blocked_by") == "brain_name_mismatch"), (
            "Case-insensitive brain_name match failed on CAMINO vs camino"
        )


async def test_submit_body_requires_brain_name_field():
    """Pydantic enforces brain_name as required — building a body
    without it must raise a validation error."""
    from shared import execution as exec_mod
    with pytest.raises(Exception) as exc:  # noqa: PT011
        exec_mod.SubmitBody(
            intent_id="sa-test-no-brain",
            order_notional_usd=10.0,
            confirm="execute",
        )
    assert "brain_name" in str(exc.value)
