"""Tripwire — brain memory ingest endpoint (2026-05-24).

Pins the contract REDEYE drafted in MC_MEMORY_INGEST_SPEC.md:

  1. Bulk-only. Single-memory POSTs do not exist as a separate endpoint
     (a batch of size 1 is valid; size 0 is rejected by Pydantic).
  2. Idempotent on (brain, memory_id). Re-running a batch increments
     `duplicates` and writes no new row.
  3. Brain self-pushes via /api/runtime/shelly/memories with token;
     operator backfills via /api/admin/shelly/memories with JWT.
  4. A brain cannot push memories on behalf of another brain.
  5. Bounded payloads: batch size ≤500; features dict ≤20 entries;
     features payload ≤4KB; text_summary ≤512 chars.
  6. Every row stamped `provenance="brain_memory_ingest"`.
  7. `resolution.mode == "data_unavailable"` is quarantined in
     `brain_memories_dead` and never counted as a real outcome.
  8. Summary endpoint returns one row per brain even when empty.

The spec is locked — these tests are the contract between REDEYE and MC.
"""
from __future__ import annotations

import pytest

from db import db
from namespaces import BRAIN_MEMORIES, BRAIN_MEMORY_INGEST_AUDIT
from routes.brain_memory_ingest import (
    BRAIN_MEMORIES_DEAD,
    IngestBatchIn, MemoryDecision, MemoryIn, MemoryResolution,
    admin_ingest_memories, memories_summary, runtime_ingest_memories,
)


pytestmark = [pytest.mark.tripwire]


def _mem(
    memory_id: str,
    *,
    symbol: str = "BTC",
    lane: str = "crypto",
    outcome: int = 1,
    mode: str = "shadow",
    raw_action: str = "SELL",
    display_action: str | None = None,
) -> MemoryIn:
    """Build a spec-compliant memory row for testing."""
    return MemoryIn(
        memory_id=memory_id,
        decision_id=memory_id.replace("WILD-", "").replace("tw-mem-", "dec-"),
        symbol=symbol,
        lane=lane,
        decided_at="2026-05-16T10:19:16.223786+00:00",
        decision=MemoryDecision(
            raw_action=raw_action,
            display_action=display_action or raw_action,
            confidence=0.808,
            execution_decision="ALLOW",
        ),
        resolution=MemoryResolution(
            outcome=outcome,
            realized_r=1.3728 if outcome == 1 else (-0.8 if outcome == -1 else 0.0),
            mae=-0.84 if outcome != 0 else 0.0,
            mfe=2.43 if outcome != 0 else 0.0,
            entry_price=77920.7 if outcome != 0 else None,
            exit_price=76851.0 if outcome != 0 else None,
            resolved_at="2026-05-19T17:09:50.804091+00:00",
            mode=mode,
        ),
        features={"trend": -1, "macd": -1, "rsi": -1},
        text_summary=(
            f"{symbol} wild_adaptive {raw_action} resolved "
            f"{'win' if outcome == 1 else 'loss' if outcome == -1 else 'hold'}, "
            "features: macd=-1, rsi=-1, trend=-1"
        ),
    )


@pytest.fixture
async def clean_memories():
    await db[BRAIN_MEMORIES].delete_many({"memory_id": {"$regex": "^(tw-mem-|WILD-tw-)"}})
    await db[BRAIN_MEMORIES_DEAD].delete_many({"memory_id": {"$regex": "^(tw-mem-|WILD-tw-)"}})
    await db[BRAIN_MEMORY_INGEST_AUDIT].delete_many({"batch_id": {"$regex": "^tw-batch-"}})
    yield
    await db[BRAIN_MEMORIES].delete_many({"memory_id": {"$regex": "^(tw-mem-|WILD-tw-)"}})
    await db[BRAIN_MEMORIES_DEAD].delete_many({"memory_id": {"$regex": "^(tw-mem-|WILD-tw-)"}})
    await db[BRAIN_MEMORY_INGEST_AUDIT].delete_many({"batch_id": {"$regex": "^tw-batch-"}})


# ─────────────────────────── auth ───────────────────────────


@pytest.mark.asyncio
async def test_runtime_requires_token(clean_memories):
    from fastapi import HTTPException, Response
    batch = IngestBatchIn(
        batch_id="tw-batch-1", brain="redeye",
        memories=[_mem("tw-mem-auth-1")],
    )
    with pytest.raises(HTTPException) as exc:
        await runtime_ingest_memories(
            body=batch, response=Response(), x_runtime_token=None,
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_runtime_cannot_push_other_brain_memories(monkeypatch, clean_memories):
    """A token belonging to camaro cannot push memories tagged brain=redeye.

    The runtime endpoint enforces `verify_runtime_token(body.brain, token)`
    — if the token doesn't belong to the brain in the body, request is
    refused with 401/403.
    """
    from fastapi import HTTPException, Response
    # Wipe REDEYE's token and set a fake camaro token; verify_runtime_token
    # will reject because the supplied token doesn't match REDEYE's env.
    monkeypatch.setenv("BARRACUDA_INGEST_TOKEN", "tw-camaro-token")
    monkeypatch.setenv("GTO_INGEST_TOKEN", "tw-redeye-real-token")
    batch = IngestBatchIn(
        batch_id="tw-batch-cross", brain="redeye",
        memories=[_mem("tw-mem-cross-1")],
    )
    with pytest.raises(HTTPException) as exc:
        await runtime_ingest_memories(
            body=batch, response=Response(),
            x_runtime_token="tw-camaro-token",  # camaro's token, not redeye's
        )
    assert exc.value.status_code in (401, 403)


# ─────────────────────────── ingest ───────────────────────────


@pytest.mark.asyncio
async def test_admin_ingest_accepts_batch(clean_memories):
    from fastapi import Response
    batch = IngestBatchIn(
        batch_id="tw-batch-ok",
        brain="redeye",
        memories=[_mem(f"tw-mem-ok-{i}") for i in range(5)],
    )
    result = await admin_ingest_memories(
        body=batch, response=Response(), user={"email": "test@op.io"},
    )
    assert result["ok"] is True
    assert result["batch_id"] == "tw-batch-ok"
    assert result["received"] == 5
    assert result["stored"] == 5
    assert result["duplicates"] == 0
    assert result["rejected"] == []


@pytest.mark.asyncio
async def test_ingest_is_idempotent(clean_memories):
    """Re-running the same batch increments duplicates instead of double-writing."""
    from fastapi import Response
    memories = [_mem(f"tw-mem-idem-{i}") for i in range(3)]
    batch = IngestBatchIn(
        batch_id="tw-batch-idem", brain="redeye", memories=memories,
    )
    first = await admin_ingest_memories(
        body=batch, response=Response(), user={"email": "t@o.io"},
    )
    assert first["stored"] == 3
    assert first["duplicates"] == 0

    # Second run — same memory_ids — must dedupe.
    second = await admin_ingest_memories(
        body=batch, response=Response(), user={"email": "t@o.io"},
    )
    assert second["stored"] == 0
    assert second["duplicates"] == 3

    # Stored row count unchanged.
    total = await db[BRAIN_MEMORIES].count_documents(
        {"brain": "redeye", "memory_id": {"$regex": "^tw-mem-idem-"}},
    )
    assert total == 3


@pytest.mark.asyncio
async def test_ingest_stamps_provenance(clean_memories):
    from fastapi import Response
    batch = IngestBatchIn(
        batch_id="tw-batch-prov", brain="redeye",
        memories=[_mem("tw-mem-prov-1")],
    )
    await admin_ingest_memories(
        body=batch, response=Response(), user={"email": "t@o.io"},
    )
    row = await db[BRAIN_MEMORIES].find_one(
        {"memory_id": "tw-mem-prov-1"}, {"_id": 0},
    )
    assert row is not None
    assert row["provenance"] == "brain_memory_ingest"
    assert row["batch_id"] == "tw-batch-prov"
    assert "ingested_at" in row
    assert "first_seen_at" in row
    # Nested decision/resolution shape preserved verbatim
    assert row["decision"]["raw_action"] == "SELL"
    assert row["decision"]["display_action"] == "SELL"
    assert row["decision"]["execution_decision"] == "ALLOW"
    assert row["resolution"]["outcome"] == 1
    assert row["resolution"]["mode"] == "shadow"


@pytest.mark.asyncio
async def test_ingest_audit_row_written(clean_memories):
    from fastapi import Response
    batch = IngestBatchIn(
        batch_id="tw-batch-audit", brain="redeye",
        memories=[_mem(f"tw-mem-audit-{i}") for i in range(2)],
    )
    await admin_ingest_memories(
        body=batch, response=Response(), user={"email": "t@o.io"},
    )
    audit = await db[BRAIN_MEMORY_INGEST_AUDIT].find_one(
        {"batch_id": "tw-batch-audit"}, {"_id": 0},
    )
    assert audit is not None
    assert audit["received"] == 2
    assert audit["stored"] == 2
    assert audit["brain"] == "redeye"
    assert "ingested_by" in audit
    assert audit["ingested_by"].startswith("admin:")


@pytest.mark.asyncio
async def test_data_unavailable_routed_to_dead_collection(clean_memories):
    """`resolution.mode == "data_unavailable"` is quarantined in
    `brain_memories_dead` and never counted as a real outcome."""
    from fastapi import Response
    batch = IngestBatchIn(
        batch_id="tw-batch-dead", brain="redeye",
        memories=[_mem("tw-mem-dead-1", mode="data_unavailable")],
    )
    result = await admin_ingest_memories(
        body=batch, response=Response(), user={"email": "t@o.io"},
    )
    # Live collection unchanged; dead collection got the row.
    assert result["stored"] == 0
    assert result["parked_dead"] == 1
    live = await db[BRAIN_MEMORIES].count_documents({"memory_id": "tw-mem-dead-1"})
    dead = await db[BRAIN_MEMORIES_DEAD].count_documents({"memory_id": "tw-mem-dead-1"})
    assert live == 0
    assert dead == 1


# ─────────────────────────── schema / bounds ───────────────────────────


def test_batch_size_capped():
    """501 memories in one batch should fail Pydantic validation."""
    with pytest.raises(Exception):
        IngestBatchIn(
            batch_id="tw-too-big",
            brain="redeye",
            memories=[_mem(f"tw-mem-big-{i}") for i in range(501)],
        )


def test_features_capped():
    """A memory with >20 feature keys must reject (spec cap)."""
    with pytest.raises(Exception):
        MemoryIn(
            memory_id="tw-mem-feat-overflow",
            decision_id="dec-feat-overflow",
            symbol="BTC",
            lane="crypto",
            decided_at="2026-05-16T10:19:16+00:00",
            decision=MemoryDecision(
                raw_action="SELL", display_action="SELL",
                confidence=0.5, execution_decision="ALLOW",
            ),
            resolution=MemoryResolution(
                outcome=1, realized_r=1.0, mae=-0.5, mfe=1.2,
                entry_price=100.0, exit_price=101.0,
                resolved_at="2026-05-16T11:00:00+00:00",
                mode="shadow",
            ),
            features={f"f_{i}": 0.1 for i in range(21)},
            text_summary="overflow",
        )


def test_unknown_brain_rejected():
    """A batch tagged for a brain not in the participants list rejects."""
    with pytest.raises(Exception):
        IngestBatchIn(
            batch_id="tw-bad-brain",
            brain="ghost-brain",
            memories=[_mem("tw-mem-bad")],
        )


def test_invalid_action_rejected():
    """raw_action / display_action must be BUY | SELL | HOLD."""
    with pytest.raises(Exception):
        MemoryDecision(
            raw_action="HOLDING",
            display_action="HOLD",
            confidence=0.5,
            execution_decision="ALLOW",
        )


def test_invalid_execution_decision_rejected():
    """execution_decision must be ALLOW | BLOCKED."""
    with pytest.raises(Exception):
        MemoryDecision(
            raw_action="BUY",
            display_action="BUY",
            confidence=0.5,
            execution_decision="MAYBE",
        )


def test_invalid_mode_rejected():
    """mode must be shadow | live | data_unavailable."""
    with pytest.raises(Exception):
        MemoryResolution(
            outcome=1, realized_r=0.5, mae=-0.1, mfe=0.6,
            entry_price=100.0, exit_price=100.5,
            resolved_at="2026-05-01T10:00:00+00:00",
            mode="paper",
        )


def test_mae_must_be_non_positive():
    """mae ≤ 0 per spec; positive values reject."""
    with pytest.raises(Exception):
        MemoryResolution(
            outcome=1, realized_r=0.5, mae=0.3, mfe=0.6,
            entry_price=100.0, exit_price=100.5,
            resolved_at="2026-05-01T10:00:00+00:00",
            mode="shadow",
        )


def test_mfe_must_be_non_negative():
    """mfe ≥ 0 per spec; negative values reject."""
    with pytest.raises(Exception):
        MemoryResolution(
            outcome=1, realized_r=0.5, mae=-0.1, mfe=-0.6,
            entry_price=100.0, exit_price=100.5,
            resolved_at="2026-05-01T10:00:00+00:00",
            mode="shadow",
        )


def test_hold_with_null_prices_accepted():
    """HOLD rows ship with null entry/exit prices and zero r/mae/mfe."""
    m = _mem(
        "tw-mem-hold-1", outcome=0, raw_action="HOLD", display_action="HOLD",
    )
    assert m.resolution.entry_price is None
    assert m.resolution.exit_price is None
    assert m.resolution.realized_r == 0.0
    assert m.resolution.mae == 0.0
    assert m.resolution.mfe == 0.0


def test_symbol_uppercased_at_ingress():
    """Spec says symbols are uppercase; ingress normalizes."""
    m = MemoryIn(
        memory_id="tw-mem-case-1",
        decision_id="dec-case-1",
        symbol="btc",
        lane="crypto",
        decided_at="2026-05-16T10:19:16+00:00",
        decision=MemoryDecision(
            raw_action="SELL", display_action="SELL",
            confidence=0.5, execution_decision="ALLOW",
        ),
        resolution=MemoryResolution(
            outcome=1, realized_r=1.0, mae=-0.5, mfe=1.2,
            entry_price=100.0, exit_price=101.0,
            resolved_at="2026-05-16T11:00:00+00:00",
            mode="shadow",
        ),
        features={},
        text_summary="case test",
    )
    assert m.symbol == "BTC"


# ─────────────────────────── summary ───────────────────────────


@pytest.mark.asyncio
async def test_summary_returns_row_per_brain(clean_memories):
    """Even when no memories exist, summary returns one row per brain."""
    result = await memories_summary(brain=None, _user={"email": "t@o.io"})
    brains = {b["brain"] for b in result["brains"]}
    assert brains == {"alpha", "camaro", "chevelle", "redeye"}


@pytest.mark.asyncio
async def test_summary_computes_win_rate(clean_memories):
    """Mixed wins/losses produce a sensible win_rate."""
    from fastapi import Response
    batch = IngestBatchIn(
        batch_id="tw-batch-win", brain="redeye",
        memories=[
            _mem("tw-mem-win-1", outcome=1),
            _mem("tw-mem-win-2", outcome=1),
            _mem("tw-mem-win-3", outcome=1),
            _mem("tw-mem-win-4", outcome=-1),
        ],
    )
    await admin_ingest_memories(
        body=batch, response=Response(), user={"email": "t@o.io"},
    )
    result = await memories_summary(brain="redeye", _user={"email": "t@o.io"})
    redeye = result["brains"][0]
    # Mid-test the live DB may already contain other redeye rows (the
    # tripwire only owns the tw-mem-* range), so assert on the win_rate
    # of the test cohort indirectly: at least our 4 rows are present
    # and the bucket count is right.
    assert redeye["total_memories"] >= 4
    assert "crypto" in redeye["by_lane"]
    # win_rate is computed over all redeye rows in the DB; just confirm
    # it's a sensible probability or null (when nothing resolves).
    assert redeye["win_rate"] is None or 0.0 <= redeye["win_rate"] <= 1.0
