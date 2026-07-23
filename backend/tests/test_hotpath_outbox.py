"""Durable SQLite Atlas outbox — enqueue/drain/retry/dead-letter."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.hotpath import outbox as ob


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path):
    ob.reset_for_tests(tmp_path / "outbox_test.sqlite")
    saved = dict(ob.HANDLERS)
    ob.HANDLERS.clear()
    yield
    ob.HANDLERS.clear()
    ob.HANDLERS.update(saved)
    ob.reset_for_tests("/app/backend/data/hotpath.sqlite")


def test_enqueue_is_idempotent_by_event_id():
    ob.enqueue("exit_outcome", "plan-1", {"a": 1})
    ob.enqueue("exit_outcome", "plan-1", {"a": 2})  # duplicate → ignored
    s = ob.get_status()
    assert s["pending"] == 1


@pytest.mark.asyncio
async def test_drain_applies_handler_and_acks():
    seen = []

    async def handler(eid, payload):
        seen.append((eid, payload))

    ob.register_handler("exit_outcome", handler)
    ob.enqueue("exit_outcome", "plan-2", {"pnl": 1.5})
    res = await ob.drain_once()
    assert res == {"selected": 1, "applied": 1, "failed": 0}
    assert seen == [("exit_outcome:plan-2", {"pnl": 1.5})]
    s = ob.get_status()
    assert s["pending"] == 0 and s["acked_total"] == 1
    # Re-drain: acked events never reprocessed.
    res2 = await ob.drain_once()
    assert res2["selected"] == 0


@pytest.mark.asyncio
async def test_failure_sets_backoff_and_retry_state():
    async def bad(eid, payload):
        raise RuntimeError("atlas down")

    ob.register_handler("exit_receipt", bad)
    ob.enqueue("exit_receipt", "r-1", {"x": 1})
    res = await ob.drain_once()
    assert res["failed"] == 1
    s = ob.get_status()
    assert s["pending"] == 1
    assert "atlas down" in (s["last_error"] or "")
    # Backoff: next_attempt_at in the future → immediate re-drain skips it.
    res2 = await ob.drain_once()
    assert res2["selected"] == 0


@pytest.mark.asyncio
async def test_dead_letter_after_max_attempts_and_operator_reset():
    async def bad(eid, payload):
        raise RuntimeError("permanent failure")

    ob.register_handler("exit_outcome", bad)
    ob.enqueue("exit_outcome", "plan-3", {"x": 1})
    conn = ob._connect()
    # Force through the attempt budget without waiting out backoff.
    for _ in range(ob.MAX_ATTEMPTS):
        with conn:
            conn.execute("UPDATE atlas_outbox SET next_attempt_at=NULL")
        await ob.drain_once()
    s = ob.get_status()
    assert s["dead_letter"] == 1 and s["pending"] == 0
    dl = ob.dead_letters()
    assert dl[0]["aggregate_id"] == "plan-3"
    assert ob.retry_dead_letters() == 1
    s2 = ob.get_status()
    assert s2["dead_letter"] == 0 and s2["pending"] == 1


@pytest.mark.asyncio
async def test_missing_handler_counts_as_failure_not_crash():
    ob.enqueue("unknown_event", "agg-1", {})
    res = await ob.drain_once()
    assert res["failed"] == 1
    assert "no handler" in (ob.get_status()["last_error"] or "")


def test_status_shape():
    s = ob.get_status()
    for k in ("pending", "dead_letter", "acked_total", "oldest_pending_at",
              "last_error", "max_attempts", "writer", "db_path"):
        assert k in s


@pytest.mark.asyncio
async def test_exit_outcome_handler_end_to_end_idempotent():
    """Real handler path: outbox event → record_outcome → single Mongo
    row even when the event is applied twice (replay safety)."""
    from db import db
    from shared.exits.outcomes import EXIT_OUTCOMES
    from shared.hotpath.handlers import register_all

    sym = "OBOX/USD"
    await db[EXIT_OUTCOMES].delete_many({"symbol": sym})
    register_all()
    plan = {
        "plan_id": "obox-1", "lane": "crypto", "symbol": sym,
        "entry_price": 100.0, "exit_price_est": 108.0, "qty_held": 1.0,
        "exit_reason": "take_profit", "origin_stack": None,
        "origin_intent_id": None, "levels_source": "lane_default",
        "adopted_at": "2026-07-23T00:00:00+00:00",
        "close_detail": "exit_order_filled",
    }
    try:
        ob.enqueue("exit_outcome", plan["plan_id"], plan)
        await ob.drain_once()
        # Replay the same event (simulate crash-between-apply-and-ack).
        conn = ob._connect()
        with conn:
            conn.execute("UPDATE atlas_outbox SET atlas_acked_at=NULL")
        await ob.drain_once()
        rows = await db[EXIT_OUTCOMES].find({"symbol": sym}).to_list(10)
        assert len(rows) == 1, "replayed event must not duplicate the outcome"
        assert rows[0]["outcome"] == "tp_hit"
    finally:
        await db[EXIT_OUTCOMES].delete_many({"symbol": sym})
