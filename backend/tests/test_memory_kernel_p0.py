"""Memory Kernel P0 — provenance, oracle, gate, training-fetch tests.

These tests pin the load-bearing invariants of the kernel:

    * diagnostic / dissent / governance → DI, never trainable
    * execution without broker+receipt consensus → UV, never trainable
    * execution with broker+receipt consensus → VE, routable to training
    * fetch_and_lock_trainable returns ONLY VE
    * confirm_training_complete refuses anything that is not VE
"""
from __future__ import annotations

import pytest

from services.memory_kernel import (
    KernelGate,
    MemoryKernelLedger,
    Provenance,
)


# ───── minimal in-memory Mongo stand-in ──────────────────────────────


class FakeResult:
    def __init__(self, modified_count: int = 0):
        self.modified_count = modified_count


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs
        self._limit = len(docs)

    def limit(self, n):
        self._limit = n
        return self

    async def to_list(self, length):
        return self.docs[: min(length, self._limit)]


def _match(d, query):
    for k, v in query.items():
        if isinstance(v, dict):
            if "$exists" in v:
                if (k in d) != v["$exists"]:
                    return False
            elif "$in" in v:
                if d.get(k) not in v["$in"]:
                    return False
        elif d.get(k) != v:
            return False
    return True


class FakeCollection:
    def __init__(self):
        self.docs = []

    async def insert_one(self, doc):
        self.docs.append(doc)
        return True

    async def find_one(self, query):
        for d in self.docs:
            if _match(d, query):
                return d
        return None

    def find(self, query):
        return FakeCursor([d for d in self.docs if _match(d, query)])

    async def update_many(self, query, update, limit=None):
        count = 0
        for d in self.docs:
            if not _match(d, query):
                continue
            for k, val in update.get("$set", {}).items():
                d[k] = val
            for k in update.get("$unset", {}).keys():
                d.pop(k, None)
            count += 1
        return FakeResult(count)

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _match(d, query):
                for k, val in update.get("$set", {}).items():
                    d[k] = val
                for k in update.get("$unset", {}).keys():
                    d.pop(k, None)
                return FakeResult(1)
        if upsert:
            new_doc = {}
            for k, v in query.items():
                if not isinstance(v, dict):
                    new_doc[k] = v
            for k, val in update.get("$set", {}).items():
                new_doc[k] = val
            self.docs.append(new_doc)
            return FakeResult(1)
        return FakeResult(0)


class FakeDB:
    def __init__(self):
        self.memory_kernel_ledger = FakeCollection()
        self.memory_kernel_quarantine = FakeCollection()
        self.memory_kernel_routes = FakeCollection()
        self.memory_kernel_reclassifications = FakeCollection()
        self.broker_orders = FakeCollection()
        self.execution_receipts = FakeCollection()


# ───── tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_diagnostic_memory_is_di_and_cannot_train():
    db = FakeDB()
    ledger = MemoryKernelLedger(db)

    doc = await ledger.submit_memory(
        source_stack="redeye",
        memory_type="diagnostic",
        payload={"note": "governance review"},
    )

    assert doc["provenance"] == Provenance.DI.value
    assert doc["trainable"] is False

    gate = KernelGate(db)
    routed = await gate.route(
        memory_id=doc["memory_id"],
        from_component="redeye_shelly",
        to_component="training_pipeline",
    )

    assert routed["allowed"] is False
    assert routed["reason"] == "non_ve_training_blocked"


@pytest.mark.asyncio
async def test_dissent_and_governance_classified_as_di():
    db = FakeDB()
    ledger = MemoryKernelLedger(db)

    dissent = await ledger.submit_memory(
        source_stack="chevelle",
        memory_type="council_dissent",
        payload={"reason": "spread floor too tight"},
    )
    governance = await ledger.submit_memory(
        source_stack="chevelle",
        memory_type="governance_review",
        payload={"note": "policy v3 approved"},
    )

    assert dissent["provenance"] == Provenance.DI.value
    assert governance["provenance"] == Provenance.DI.value


@pytest.mark.asyncio
async def test_replay_and_backtest_classified_as_so():
    db = FakeDB()
    ledger = MemoryKernelLedger(db)

    sim = await ledger.submit_memory(
        source_stack="alpha",
        memory_type="replay",
        payload={"symbol": "AAPL"},
    )

    assert sim["provenance"] == Provenance.SO.value
    assert sim["trainable"] is False


@pytest.mark.asyncio
async def test_execution_without_receipt_becomes_uv():
    db = FakeDB()
    ledger = MemoryKernelLedger(db)

    doc = await ledger.submit_memory(
        source_stack="camaro",
        memory_type="execution",
        payload={
            "symbol": "BTC-USD",
            "broker_order_id": "bo_1",
            "filled_qty": 1,
        },
        requested_provenance="VE",  # stack requests VE — MC denies
    )

    assert doc["provenance"] == Provenance.UV.value
    assert doc["trainable"] is False
    # quarantine log written
    assert len(db.memory_kernel_quarantine.docs) == 1


@pytest.mark.asyncio
async def test_execution_with_consensus_becomes_ve():
    db = FakeDB()

    await db.broker_orders.insert_one({
        "broker_order_id": "bo_1",
        "symbol": "BTC-USD",
        "status": "FILLED",
        "filled_qty": 1,
    })
    await db.execution_receipts.insert_one({
        "receipt_id": "rx_1",
        "symbol": "BTC-USD",
        "status": "FILLED",
        "filled_qty": 1,
    })

    ledger = MemoryKernelLedger(db)
    doc = await ledger.submit_memory(
        source_stack="redeye",
        memory_type="execution",
        payload={
            "symbol": "BTC-USD",
            "broker_order_id": "bo_1",
            "execution_receipt_id": "rx_1",
            "filled_qty": 1,
        },
    )

    assert doc["provenance"] == Provenance.VE.value
    assert doc["trainable"] is True
    assert doc["payload"]["settlement_oracle"]["settled"] is True

    gate = KernelGate(db)
    routed = await gate.route(
        memory_id=doc["memory_id"],
        from_component="memory_kernel",
        to_component="training_pipeline",
    )

    assert routed["allowed"] is True


@pytest.mark.asyncio
async def test_execution_symbol_mismatch_becomes_uv():
    db = FakeDB()

    await db.broker_orders.insert_one({
        "broker_order_id": "bo_1",
        "symbol": "BTC-USD",
        "status": "FILLED",
        "filled_qty": 1,
    })
    await db.execution_receipts.insert_one({
        "receipt_id": "rx_1",
        "symbol": "ETH-USD",  # mismatch
        "status": "FILLED",
        "filled_qty": 1,
    })

    ledger = MemoryKernelLedger(db)
    doc = await ledger.submit_memory(
        source_stack="redeye",
        memory_type="execution",
        payload={
            "symbol": "BTC-USD",
            "broker_order_id": "bo_1",
            "execution_receipt_id": "rx_1",
            "filled_qty": 1,
        },
    )

    assert doc["provenance"] == Provenance.UV.value


@pytest.mark.asyncio
async def test_execution_qty_mismatch_becomes_uv():
    db = FakeDB()

    await db.broker_orders.insert_one({
        "broker_order_id": "bo_1",
        "symbol": "BTC-USD",
        "status": "FILLED",
        "filled_qty": 1,
    })
    await db.execution_receipts.insert_one({
        "receipt_id": "rx_1",
        "symbol": "BTC-USD",
        "status": "FILLED",
        "filled_qty": 2,  # mismatch
    })

    ledger = MemoryKernelLedger(db)
    doc = await ledger.submit_memory(
        source_stack="redeye",
        memory_type="execution",
        payload={
            "symbol": "BTC-USD",
            "broker_order_id": "bo_1",
            "execution_receipt_id": "rx_1",
            "filled_qty": 1,
        },
    )

    assert doc["provenance"] == Provenance.UV.value


@pytest.mark.asyncio
async def test_route_to_execution_blocks_non_ve_and_logs_critical_alert():
    db = FakeDB()
    ledger = MemoryKernelLedger(db)

    di_doc = await ledger.submit_memory(
        source_stack="alpha",
        memory_type="diagnostic",
        payload={"note": "x"},
    )

    gate = KernelGate(db)
    routed = await gate.route(
        memory_id=di_doc["memory_id"],
        from_component="alpha_shelly",
        to_component="execution_engine",
    )

    assert routed["allowed"] is False
    assert routed["reason"] == "non_ve_execution_blocked"

    # critical alert was written to quarantine
    alerts = [
        d for d in db.memory_kernel_quarantine.docs
        if d.get("alert_level") == "CRITICAL"
    ]
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_unknown_memory_id_is_blocked():
    db = FakeDB()
    gate = KernelGate(db)
    routed = await gate.route(
        memory_id="does-not-exist",
        from_component="x",
        to_component="training_pipeline",
    )
    assert routed["allowed"] is False
    assert routed["reason"] == "unknown_memory"


@pytest.mark.asyncio
async def test_critic_and_diagnostic_destinations_allowed():
    db = FakeDB()
    ledger = MemoryKernelLedger(db)
    doc = await ledger.submit_memory(
        source_stack="redeye",
        memory_type="diagnostic",
        payload={"note": "x"},
    )
    gate = KernelGate(db)
    for dest in ("adversarial_critic", "governance_reviewer", "diagnostics"):
        routed = await gate.route(
            memory_id=doc["memory_id"],
            from_component="auditor",
            to_component=dest,
        )
        assert routed["allowed"] is True


@pytest.mark.asyncio
async def test_fetch_trainable_only_returns_ve():
    db = FakeDB()
    ledger = MemoryKernelLedger(db)

    await db.memory_kernel_ledger.insert_one({
        "memory_id": "ve1",
        "provenance": "VE",
        "trainable": True,
        "used_in_training": False,
    })
    await db.memory_kernel_ledger.insert_one({
        "memory_id": "di1",
        "provenance": "DI",
        "trainable": False,
        "used_in_training": False,
    })

    out = await ledger.fetch_and_lock_trainable(min_samples=1, limit=10)

    assert out["ok"] is True
    assert out["count"] == 1
    assert out["memories"][0]["memory_id"] == "ve1"
    # lock applied
    assert out["memories"][0]["training_lock"] == out["lock_id"]


@pytest.mark.asyncio
async def test_fetch_trainable_below_min_samples_returns_empty():
    db = FakeDB()
    ledger = MemoryKernelLedger(db)

    await db.memory_kernel_ledger.insert_one({
        "memory_id": "ve1",
        "provenance": "VE",
        "trainable": True,
        "used_in_training": False,
    })

    out = await ledger.fetch_and_lock_trainable(min_samples=5, limit=10)
    assert out["ok"] is False
    assert out["reason"] == "insufficient_ve_samples"
    assert out["memories"] == []


@pytest.mark.asyncio
async def test_confirm_training_refuses_non_ve():
    """The axiom: refusing to train on non-verified memory."""
    db = FakeDB()
    ledger = MemoryKernelLedger(db)

    await db.memory_kernel_ledger.insert_one({
        "memory_id": "bad1",
        "provenance": "DI",
        "training_lock": "lock1",
    })

    with pytest.raises(RuntimeError, match="non-verified"):
        await ledger.confirm_training_complete(
            memory_ids=["bad1"],
            lock_id="lock1",
        )


@pytest.mark.asyncio
async def test_confirm_training_marks_ve_used():
    db = FakeDB()
    ledger = MemoryKernelLedger(db)

    await db.memory_kernel_ledger.insert_one({
        "memory_id": "ve1",
        "provenance": "VE",
        "trainable": True,
        "used_in_training": False,
        "training_lock": "lockA",
    })

    out = await ledger.confirm_training_complete(
        memory_ids=["ve1"],
        lock_id="lockA",
    )
    assert out["ok"] is True
    assert out["modified_count"] == 1

    doc = await db.memory_kernel_ledger.find_one({"memory_id": "ve1"})
    assert doc["used_in_training"] is True
    assert "training_lock" not in doc


# ───── tripwire surface ──────────────────────────────────────────────


@pytest.mark.tripwire
def test_provenance_enum_locked():
    """Only these four classes exist. Anything else is a doctrine break."""
    assert {p.value for p in Provenance} == {"VE", "SO", "DI", "UV"}


@pytest.mark.tripwire
@pytest.mark.asyncio
async def test_axiom_string_is_load_bearing():
    """
    The axiom in `confirm_training_complete()` must remain the load-bearing
    wall — the test guarantees the function raises with a recognisable
    message any time non-VE memory is presented to the training pipeline.
    """
    db = FakeDB()
    ledger = MemoryKernelLedger(db)
    await db.memory_kernel_ledger.insert_one({
        "memory_id": "uv1",
        "provenance": "UV",
        "training_lock": "L",
    })
    with pytest.raises(RuntimeError) as exc:
        await ledger.confirm_training_complete(memory_ids=["uv1"], lock_id="L")
    assert "non-verified memory" in str(exc.value)
