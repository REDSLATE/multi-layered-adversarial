"""Reclassification doctrine — UV → SO only.

Locked rules:
  * UV → SO   ✓ (operator-driven; append-only audit)
  * UV → VE   ✗ (would let an orphan poison training — forbidden)
  * SO → VE   ✗ (only the SettlementOracle can mint VE)
  * VE → *    ✗ (verified facts are append-only)
  * DI → *    ✗ (diagnostics never become trade evidence)

Anything that breaks these is a kernel axiom failure.
"""
from __future__ import annotations

import pytest

from services.memory_kernel import (
    KernelGate,
    MemoryKernelLedger,
    Provenance,
)
from tests.test_memory_kernel_p0 import FakeDB


@pytest.mark.asyncio
async def test_uv_to_so_promotion_succeeds():
    db = FakeDB()
    ledger = MemoryKernelLedger(db)

    # Orphan execution → UV
    doc = await ledger.submit_memory(
        source_stack="alpaca_orphan",
        memory_type="execution",
        payload={
            "symbol": "NVDA",
            "broker_order_id": "bo_orphan_1",
            "filled_qty": 0.32,
        },
    )
    assert doc["provenance"] == Provenance.UV.value

    out = await ledger.reclassify_uv_to_so(
        memory_id=doc["memory_id"],
        operator="admin@risedual.io",
        reason="Replay-only corpus for RoadGuard calibration",
    )

    assert out["ok"] is True
    assert out["from"] == Provenance.UV.value
    assert out["to"] == Provenance.SO.value

    mem = await db.memory_kernel_ledger.find_one({"memory_id": doc["memory_id"]})
    assert mem["provenance"] == Provenance.SO.value
    assert mem["trainable"] is False
    assert mem["reclassified_from"] == Provenance.UV.value
    assert mem["reclassification_id"] == out["reclassification_id"]

    # Audit row written
    audit = await db.memory_kernel_reclassifications.find_one(
        {"memory_id": doc["memory_id"]}
    )
    assert audit is not None
    assert audit["operator"] == "admin@risedual.io"
    assert "calibration" in audit["reason"].lower()


@pytest.mark.asyncio
async def test_idempotent_re_promotion_is_noop():
    db = FakeDB()
    ledger = MemoryKernelLedger(db)

    doc = await ledger.submit_memory(
        source_stack="alpaca_orphan",
        memory_type="execution",
        payload={"symbol": "AMZN", "broker_order_id": "bo_2", "filled_qty": 1},
    )
    await ledger.reclassify_uv_to_so(
        memory_id=doc["memory_id"], operator="x", reason="first promo",
    )
    second = await ledger.reclassify_uv_to_so(
        memory_id=doc["memory_id"], operator="x", reason="second attempt",
    )
    assert second["ok"] is True
    assert second["no_op"] is True


@pytest.mark.asyncio
@pytest.mark.tripwire
async def test_reclassification_to_ve_is_forbidden():
    """The axiom is non-negotiable. There is no UV→VE or SO→VE path."""
    db = FakeDB()
    ledger = MemoryKernelLedger(db)

    # Try to construct a sequence: orphan → SO → (illegal) VE
    doc = await ledger.submit_memory(
        source_stack="alpaca_orphan",
        memory_type="execution",
        payload={"symbol": "GOOGL", "broker_order_id": "bo_3", "filled_qty": 1},
    )
    await ledger.reclassify_uv_to_so(
        memory_id=doc["memory_id"], operator="x", reason="promote to SO",
    )

    # There is no public API to mint VE. Even the kernel's own
    # confirm_training_complete refuses non-VE memory. Verify that the
    # SO memory still triggers the axiom:
    await db.memory_kernel_ledger.update_one(
        {"memory_id": doc["memory_id"]},
        {"$set": {"training_lock": "lockX"}},
    )
    with pytest.raises(RuntimeError, match="non-verified memory"):
        await ledger.confirm_training_complete(
            memory_ids=[doc["memory_id"]],
            lock_id="lockX",
        )


@pytest.mark.asyncio
async def test_ve_cannot_be_reclassified():
    """Verified facts are append-only — you can't downgrade them."""
    db = FakeDB()
    # Seed a fully-consensus VE memory
    await db.broker_orders.insert_one({
        "broker_order_id": "bo_ve",
        "symbol": "BTC-USD", "status": "FILLED", "filled_qty": 1,
    })
    await db.execution_receipts.insert_one({
        "receipt_id": "rx_ve",
        "symbol": "BTC-USD", "status": "FILLED", "filled_qty": 1,
    })
    ledger = MemoryKernelLedger(db)
    ve = await ledger.submit_memory(
        source_stack="redeye", memory_type="execution",
        payload={"symbol": "BTC-USD", "broker_order_id": "bo_ve",
                 "execution_receipt_id": "rx_ve", "filled_qty": 1},
    )
    assert ve["provenance"] == Provenance.VE.value

    with pytest.raises(PermissionError, match="only UV"):
        await ledger.reclassify_uv_to_so(
            memory_id=ve["memory_id"], operator="x", reason="downgrade attempt",
        )


@pytest.mark.asyncio
async def test_di_cannot_be_reclassified():
    """Diagnostics are governance-only, never a trade source."""
    db = FakeDB()
    ledger = MemoryKernelLedger(db)
    di = await ledger.submit_memory(
        source_stack="chevelle", memory_type="diagnostic",
        payload={"note": "x"},
    )
    assert di["provenance"] == Provenance.DI.value
    with pytest.raises(PermissionError):
        await ledger.reclassify_uv_to_so(
            memory_id=di["memory_id"], operator="x", reason="x",
        )


@pytest.mark.asyncio
async def test_reclassification_requires_reason_and_operator():
    db = FakeDB()
    ledger = MemoryKernelLedger(db)
    doc = await ledger.submit_memory(
        source_stack="alpaca_orphan", memory_type="execution",
        payload={"symbol": "MSFT", "broker_order_id": "bo_4", "filled_qty": 1},
    )
    with pytest.raises(ValueError, match="reason"):
        await ledger.reclassify_uv_to_so(
            memory_id=doc["memory_id"], operator="x", reason="",
        )
    with pytest.raises(ValueError, match="operator"):
        await ledger.reclassify_uv_to_so(
            memory_id=doc["memory_id"], operator="", reason="ok reason",
        )


@pytest.mark.asyncio
async def test_unknown_memory_id_raises():
    db = FakeDB()
    ledger = MemoryKernelLedger(db)
    with pytest.raises(ValueError, match="not found"):
        await ledger.reclassify_uv_to_so(
            memory_id="does-not-exist", operator="x", reason="x",
        )


@pytest.mark.asyncio
async def test_so_memory_still_blocked_at_kernel_gate_for_training():
    """SO is for replay; the gate must still refuse training routes."""
    db = FakeDB()
    ledger = MemoryKernelLedger(db)
    doc = await ledger.submit_memory(
        source_stack="alpaca_orphan", memory_type="execution",
        payload={"symbol": "NVDA", "broker_order_id": "bo_5", "filled_qty": 1},
    )
    await ledger.reclassify_uv_to_so(
        memory_id=doc["memory_id"], operator="x", reason="calibration",
    )

    gate = KernelGate(db)
    routed = await gate.route(
        memory_id=doc["memory_id"],
        from_component="replay_engine",
        to_component="training_pipeline",
    )
    assert routed["allowed"] is False
    assert routed["reason"] == "non_ve_training_blocked"


@pytest.mark.tripwire
@pytest.mark.asyncio
async def test_axiom_holds_for_reclassified_so():
    """Even after UV→SO promotion, the axiom in confirm_training_complete
    must refuse the memory. SO is still not VE."""
    db = FakeDB()
    ledger = MemoryKernelLedger(db)
    doc = await ledger.submit_memory(
        source_stack="alpaca_orphan", memory_type="execution",
        payload={"symbol": "META", "broker_order_id": "bo_6", "filled_qty": 1},
    )
    await ledger.reclassify_uv_to_so(
        memory_id=doc["memory_id"], operator="x", reason="x",
    )
    # Forge a training_lock on the SO memory; confirm_training must
    # still raise.
    await db.memory_kernel_ledger.update_one(
        {"memory_id": doc["memory_id"]},
        {"$set": {"training_lock": "lockY"}},
    )
    with pytest.raises(RuntimeError, match="non-verified memory"):
        await ledger.confirm_training_complete(
            memory_ids=[doc["memory_id"]],
            lock_id="lockY",
        )
