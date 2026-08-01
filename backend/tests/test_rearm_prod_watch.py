"""Prod Deploy Watch tests (2026-08-01): child outcome classification,
local-queue membership accessor, validation-doc filter, health-check
wiring, duplicate-block recording."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.risk_sizer.rearm_report import (  # noqa: E402
    OUTCOMES, classify_child_outcome, is_validation_doc,
)

pytestmark = pytest.mark.tripwire


def _child(**kw):
    return {"intent_id": "c1", "gate_state": "pending", "executed": False,
            "ingest_ts": "2026-08-01T15:00:00+00:00", **kw}


def test_outcome_filled():
    assert classify_child_outcome(_child(executed=True)) == "filled"


def test_outcome_waiting():
    assert classify_child_outcome(_child()) == "waiting"


def test_outcome_blocked_again_internal_gates():
    for rr, br in (("entry_timing:MISSED_ENTRY_CHASE_RISK",
                    "ENTRY_TIMING_REJECTED"),
                   ("risk_sizer:not_in_buy_allowlist",
                    "RISK_SIZER_REJECTED")):
        c = _child(gate_state="blocked", risk_reason=rr, broker_reason=br)
        assert classify_child_outcome(c) == "blocked_again"


def test_outcome_broker_rejected():
    c = _child(gate_state="blocked",
               broker_reason="EQuery:Insufficient funds",
               broker_error_bucket="insufficient_funds")
    assert classify_child_outcome(c) == "broker_rejected"


def test_outcome_submitted_and_reconciliation():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    fresh = _child(gate_state="submitted",
                   last_submit_ts=now.isoformat())
    assert classify_child_outcome(fresh, now=now) == "submitted"
    stale = _child(gate_state="submitted",
                   last_submit_ts=(now - timedelta(minutes=10)).isoformat())
    assert classify_child_outcome(stale, now=now) == "reconciliation_required"


def test_outcome_expired_unrouted_needs_reconcile():
    c = _child(gate_state="expired_unrouted")
    assert classify_child_outcome(c) == "reconciliation_required"


def test_outcomes_vocabulary_stable():
    assert OUTCOMES == ("waiting", "blocked_again", "submitted", "filled",
                        "broker_rejected", "reconciliation_required")


def test_validation_doc_filter():
    assert is_validation_doc({"intent_id": "validate-abc"})
    assert is_validation_doc({"original_intent_id": "rearmval-orig-1"})
    assert is_validation_doc({"intent_id": "x", "stack": "validation"})
    assert is_validation_doc({"intent_id": "x",
                              "rationale": "VALIDATION: test"})
    assert not is_validation_doc({"intent_id": "aed3-organic",
                                  "stack": "gto", "rationale": "momentum"})


def test_intent_queue_has():
    from shared.hotpath import intent_queue
    intent_queue.reset_for_tests()
    doc = {"intent_id": "has-test-1", "ingest_ts": "2026-08-01T15:00:00+00:00",
           "lane": "crypto", "symbol": "BTC/USD", "action": "BUY",
           "gate_state": "pending", "executed": False}
    intent_queue.enqueue(doc)
    assert intent_queue.has("has-test-1")
    assert not intent_queue.has("never-enqueued")


@pytest.mark.asyncio
async def test_duplicate_block_recorded_on_existing_trigger(monkeypatch):
    """A repeated block on a WATCHING symbol must bump the counter and
    refresh the peak — not stack a second trigger."""
    from shared.risk_sizer import entry_rearm as mod
    updates, inserted = [], []

    class _Coll:
        async def find_one(self, *a, **k):
            return {"_id": "existing"}
        async def insert_one(self, doc):
            inserted.append(doc)
        async def update_one(self, q, u):
            updates.append((q, u))

    class _FakeDB(dict):
        def __getitem__(self, k):
            return _Coll()

    import db as dbmod
    monkeypatch.setattr(dbmod, "db", _FakeDB(), raising=False)

    async def _cfg():
        return {**mod.DEFAULT_REARM, "gate_enabled": True}
    monkeypatch.setattr(mod, "get_rearm_config", _cfg)

    await mod.create_trigger(
        {"action": "BUY", "symbol": "BTC/USD", "lane": "crypto",
         "intent_id": "i2"},
        "MISSED_ENTRY_CHASE_RISK",
        {"confirmation_price": 100.0, "current_price": 110.0})
    assert inserted == []
    assert len(updates) == 1
    _q, u = updates[0]
    assert u["$inc"] == {"duplicate_blocks_prevented": 1}
    assert u["$max"] == {"peak_price": 110.0}


def test_health_and_timeline_routes_registered():
    src = open("/app/backend/routes/universe_admin.py").read()
    assert '"/entry-timing/health"' in src
    assert '"/entry-timing/rearm-timeline"' in src
