"""Tripwire — brain memory ingest endpoint (2026-05-24).

Pins the contract REDEYE drafted in MC_MEMORY_INGEST_SPEC.md:

  1. Bulk-only. Single-memory POSTs do not exist.
  2. Idempotent on (brain, memory_id). Re-running a batch returns
     dedupe counts; no row is double-written.
  3. Brain self-pushes via /api/runtime/shelly/memories with token;
     operator backfills via /api/admin/shelly/memories with JWT.
  4. A brain cannot push memories on behalf of another brain.
  5. Bounded payloads: batch size <= 500; features dict <= 32 entries;
     evidence_refs <= 16 entries.
  6. Every row stamped provenance="brain_memory_ingest" so downstream
     consumers can filter trivially.
  7. Summary endpoint returns one row per brain even when empty.
"""
from __future__ import annotations

import pytest

from db import db
from namespaces import BRAIN_MEMORIES, BRAIN_MEMORY_INGEST_AUDIT
from routes.brain_memory_ingest import (
    IngestBatchIn, MemoryDecision, MemoryIn, MemoryResolution,
    admin_ingest_memories, memories_summary, runtime_ingest_memories,
)


pytestmark = [pytest.mark.tripwire]


def _mem(memory_id: str, *, symbol: str = "BTC/USD", outcome: int = 1) -> MemoryIn:
    return MemoryIn(
        memory_id=memory_id,
        symbol=symbol,
        lane="crypto",
        decision_ts="2026-05-01T10:00:00+00:00",
        decision=MemoryDecision(action="SELL", confidence=0.72),
        resolution=MemoryResolution(
            outcome=outcome, realized_r=0.34,
            mfe_pct=0.8, mae_pct=-0.2,
            resolved_at="2026-05-01T11:00:00+00:00",
        ),
        features={"regime_misalignment": 0.6, "counter_momentum": -0.4},
        evidence_refs=["chroma://mem/abc123"],
    )


@pytest.fixture
async def clean_memories():
    await db[BRAIN_MEMORIES].delete_many({"memory_id": {"$regex": "^tw-mem-"}})
    await db[BRAIN_MEMORY_INGEST_AUDIT].delete_many({"batch_id": {"$regex": "^tw-batch-"}})
    yield
    await db[BRAIN_MEMORIES].delete_many({"memory_id": {"$regex": "^tw-mem-"}})
    await db[BRAIN_MEMORY_INGEST_AUDIT].delete_many({"batch_id": {"$regex": "^tw-batch-"}})


# ─────────────────────────── auth ───────────────────────────


@pytest.mark.asyncio
async def test_runtime_requires_token(clean_memories):
    from fastapi import HTTPException
    batch = IngestBatchIn(
        batch_id="tw-batch-1", brain="redeye",
        memories=[_mem("tw-mem-auth-1")],
    )
    with pytest.raises(HTTPException) as exc:
        await runtime_ingest_memories(
            body=batch, runtime="redeye", x_runtime_token=None,
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_runtime_cannot_push_other_brain_memories(monkeypatch, clean_memories):
    """Camaro's token cannot push memories tagged brain=redeye."""
    from fastapi import HTTPException
    monkeypatch.setenv("CAMARO_INGEST_TOKEN", "tw-camaro-token")
    batch = IngestBatchIn(
        batch_id="tw-batch-cross", brain="redeye",
        memories=[_mem("tw-mem-cross-1")],
    )
    with pytest.raises(HTTPException) as exc:
        await runtime_ingest_memories(
            body=batch, runtime="camaro", x_runtime_token="tw-camaro-token",
        )
    assert exc.value.status_code == 403


# ─────────────────────────── ingest ───────────────────────────


@pytest.mark.asyncio
async def test_admin_ingest_accepts_batch(clean_memories):
    batch = IngestBatchIn(
        batch_id="tw-batch-ok",
        brain="redeye",
        memories=[_mem(f"tw-mem-ok-{i}") for i in range(5)],
    )
    result = await admin_ingest_memories(
        body=batch, user={"email": "test@op.io"},
    )
    assert result["ok"] is True
    assert result["accepted"] == 5
    assert result["deduped"] == 0
    assert result["rejected"] == 0


@pytest.mark.asyncio
async def test_ingest_is_idempotent(clean_memories):
    """Re-running the same batch deduplicates instead of double-writing."""
    memories = [_mem(f"tw-mem-idem-{i}") for i in range(3)]
    batch = IngestBatchIn(
        batch_id="tw-batch-idem", brain="redeye", memories=memories,
    )
    first = await admin_ingest_memories(body=batch, user={"email": "t@o.io"})
    assert first["accepted"] == 3
    assert first["deduped"] == 0

    # Second run — same memory_ids — must dedupe.
    second = await admin_ingest_memories(body=batch, user={"email": "t@o.io"})
    assert second["accepted"] == 0
    assert second["deduped"] == 3

    # Stored row count unchanged.
    total = await db[BRAIN_MEMORIES].count_documents(
        {"brain": "redeye", "memory_id": {"$regex": "^tw-mem-idem-"}},
    )
    assert total == 3


@pytest.mark.asyncio
async def test_ingest_stamps_provenance(clean_memories):
    batch = IngestBatchIn(
        batch_id="tw-batch-prov", brain="redeye",
        memories=[_mem("tw-mem-prov-1")],
    )
    await admin_ingest_memories(body=batch, user={"email": "t@o.io"})
    row = await db[BRAIN_MEMORIES].find_one(
        {"memory_id": "tw-mem-prov-1"}, {"_id": 0},
    )
    assert row is not None
    assert row["provenance"] == "brain_memory_ingest"
    assert row["batch_id"] == "tw-batch-prov"
    assert "ingested_at" in row
    assert "first_seen_at" in row


@pytest.mark.asyncio
async def test_ingest_audit_row_written(clean_memories):
    batch = IngestBatchIn(
        batch_id="tw-batch-audit", brain="redeye",
        memories=[_mem(f"tw-mem-audit-{i}") for i in range(2)],
    )
    await admin_ingest_memories(body=batch, user={"email": "t@o.io"})
    audit = await db[BRAIN_MEMORY_INGEST_AUDIT].find_one(
        {"batch_id": "tw-batch-audit"}, {"_id": 0},
    )
    assert audit is not None
    assert audit["accepted"] == 2
    assert audit["brain"] == "redeye"
    assert "ingested_by" in audit


# ─────────────────────────── bounds ───────────────────────────


def test_batch_size_capped():
    """501 memories in one batch should fail Pydantic validation."""
    with pytest.raises(Exception):
        IngestBatchIn(
            batch_id="tw-too-big",
            brain="redeye",
            memories=[_mem(f"tw-mem-big-{i}") for i in range(501)],
        )


def test_features_capped():
    """A memory with >32 feature keys must reject."""
    with pytest.raises(Exception):
        MemoryIn(
            memory_id="tw-mem-feat-overflow",
            symbol="BTC/USD", lane="crypto",
            decision_ts="2026-05-01T10:00:00+00:00",
            decision=MemoryDecision(action="SELL", confidence=0.5),
            resolution=MemoryResolution(
                outcome=1, resolved_at="2026-05-01T11:00:00+00:00",
            ),
            features={f"f_{i}": 0.1 for i in range(33)},
        )


def test_unknown_brain_rejected():
    """A batch tagged for a brain not in the participants list rejects."""
    with pytest.raises(Exception):
        IngestBatchIn(
            batch_id="tw-bad-brain",
            brain="ghost-brain",
            memories=[_mem("tw-mem-bad")],
        )


# ─────────────────────────── summary ───────────────────────────


@pytest.mark.asyncio
async def test_summary_returns_row_per_brain(clean_memories):
    """Even when no memories exist, summary returns one row per brain."""
    result = await memories_summary(brain=None, _user={"email": "t@o.io"})
    brains = {b["brain"] for b in result["brains"]}
    assert brains == {"alpha", "camaro", "chevelle", "redeye"}
    for b in result["brains"]:
        if b["brain"] != "redeye":
            assert b["total_memories"] == 0
            assert b["win_rate"] is None


@pytest.mark.asyncio
async def test_summary_computes_win_rate(clean_memories):
    """Mixed wins/losses produce a sensible win_rate."""
    batch = IngestBatchIn(
        batch_id="tw-batch-win", brain="redeye",
        memories=[
            _mem("tw-mem-win-1", outcome=1),
            _mem("tw-mem-win-2", outcome=1),
            _mem("tw-mem-win-3", outcome=1),
            _mem("tw-mem-win-4", outcome=-1),
        ],
    )
    await admin_ingest_memories(body=batch, user={"email": "t@o.io"})
    result = await memories_summary(brain="redeye", _user={"email": "t@o.io"})
    redeye = result["brains"][0]
    assert redeye["total_memories"] == 4
    assert redeye["win_rate"] == 0.75
    assert redeye["by_lane"] == {"crypto": 4}
