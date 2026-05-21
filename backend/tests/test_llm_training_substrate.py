"""
RISE_AI training substrate — happy-path tests against the real
Mongo (test_database).

Lock invariants:
  * preference_log: invalid scores raise; valid scores persist.
  * distillation_queue: low scores skipped, missing call_id no-ops,
    idempotent re-enqueue.
  * eval_harness: agreement scoring within [0,1].
"""
from __future__ import annotations

import uuid

import pytest

from db import db
from namespaces import (
    LLM_CALLS,
    LLM_DISTILLATION_QUEUE,
    LLM_PREFERENCE_LOG,
    LLM_EVAL_RUNS,
)
from shared.llm.training.distillation_queue import (
    enqueue_training_pair,
    dequeue_training_pairs,
)
from shared.llm.training.eval_harness import _jaccard_tokens, _summarize
from shared.llm.training.preference_log import (
    VALID_SCORES,
    record_preference,
    tally_preferences,
)


# ─────────────── preference_log ───────────────────────────────────────


@pytest.mark.asyncio
async def test_preference_log_rejects_invalid_score():
    with pytest.raises(ValueError):
        await record_preference(
            call_id="x", score=99, outcome="trade_won",
        )


@pytest.mark.asyncio
async def test_preference_log_rejects_blank_call_id():
    with pytest.raises(ValueError):
        await record_preference(
            call_id="", score=1, outcome="trade_won",
        )


@pytest.mark.asyncio
async def test_preference_log_persists_row():
    call_id = f"test-pref-{uuid.uuid4()}"
    out = await record_preference(
        call_id=call_id, score=2, outcome="trade_won", note="clean fill",
    )
    assert out["call_id"] == call_id
    assert out["score"] == 2
    fetched = await db[LLM_PREFERENCE_LOG].find_one({"call_id": call_id})
    assert fetched is not None
    assert fetched["score"] == 2
    # Cleanup
    await db[LLM_PREFERENCE_LOG].delete_many({"call_id": call_id})


@pytest.mark.tripwire
def test_preference_log_valid_scores_pinned():
    assert VALID_SCORES == {-2, -1, 0, 1, 2}


@pytest.mark.asyncio
async def test_preference_log_tally_runs_without_error():
    out = await tally_preferences(window_hours=1)
    assert "rows" in out
    assert out["window_hours"] == 1


# ─────────────── distillation_queue ───────────────────────────────────


@pytest.mark.asyncio
async def test_distillation_skips_low_scores():
    result = await enqueue_training_pair(
        call_id="anything", score=0, outcome="meh",
    )
    assert result is None


@pytest.mark.asyncio
async def test_distillation_skips_missing_call():
    result = await enqueue_training_pair(
        call_id=f"definitely-missing-{uuid.uuid4()}",
        score=2, outcome="trade_won",
    )
    assert result is None


@pytest.mark.asyncio
async def test_distillation_enqueue_is_idempotent():
    call_id = f"test-dist-{uuid.uuid4()}"
    # Seed a fake ledger row.
    await db[LLM_CALLS].insert_one({
        "call_id": call_id,
        "role": "strategist",
        "task": "test",
        "provider": "openai",
        "model": "gpt-5.1",
        "prompt": "hello?",
        "response": "world.",
    })
    try:
        a = await enqueue_training_pair(
            call_id=call_id, score=2, outcome="trade_won",
        )
        b = await enqueue_training_pair(
            call_id=call_id, score=2, outcome="trade_won",
        )
        assert a is not None
        assert b is None  # idempotent: second call is a no-op
        rows = await dequeue_training_pairs(limit=10, consumer="pytest")
        assert any(r["call_id"] == call_id for r in rows)
    finally:
        await db[LLM_CALLS].delete_many({"call_id": call_id})
        await db[LLM_DISTILLATION_QUEUE].delete_many({"call_id": call_id})


# ─────────────── eval_harness pure-function tests ─────────────────────


def test_jaccard_identical_strings_is_one():
    assert _jaccard_tokens("hello world", "hello world") == 1.0


def test_jaccard_disjoint_strings_is_zero():
    assert _jaccard_tokens("foo bar", "baz qux") == 0.0


def test_jaccard_empty_both_is_one():
    assert _jaccard_tokens("", "") == 1.0


def test_jaccard_empty_one_side_is_zero():
    assert _jaccard_tokens("hello", "") == 0.0


def test_summarize_handles_empty_rows():
    s = _summarize([])
    assert s["avg_agreement"] is None
    assert s["candidate_ok_rate"] is None
    assert s["primary_ok_rate"] is None


def test_summarize_basic_aggregation():
    rows = [
        {"agreement": 1.0, "primary": {"ok": True}, "candidate": {"ok": True}},
        {"agreement": 0.5, "primary": {"ok": True}, "candidate": {"ok": False}},
    ]
    s = _summarize(rows)
    assert s["avg_agreement"] == 0.75
    assert s["primary_ok_rate"] == 1.0
    assert s["candidate_ok_rate"] == 0.5


# ─────────────── Mongo collections exist on first write ────────────────


@pytest.mark.asyncio
async def test_eval_runs_collection_is_addressable():
    """We don't actually run an evaluation here (would burn LLM
    tokens). We just verify the collection name resolves and is
    queryable — proving the namespaces wiring is consistent."""
    n = await db[LLM_EVAL_RUNS].count_documents({})
    assert n >= 0  # any non-negative count is fine
