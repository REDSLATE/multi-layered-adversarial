"""Stale-intent sweeper — archive-then-delete tests.

2026-02-19 (revised) operator directive locked in:
    * 6-hour minimum age gate
    * Preserve executed / broker_order_id / submitted intents
    * Preserve intents with active capital-ledger reservations
    * **Learning-capture requirement is CONDITIONAL** — only
      directional intents (BUY/SELL/SHORT/COVER) that actually
      reached execution (broker_order_id OR gate_state in
      {submitted, executed, broker_rejected}) require a learning
      row. HOLD/WATCH/blocked-pre-broker no_trade rows do NOT.
    * Learning-aware bifurcation (distilled → delete outright,
      not-distilled → archive-then-delete)
    * Typed archive_reason: legacy_non_learning_no_trade /
      directional_blocked_pre_broker / stale_never_reached_broker
    * Batch bounded at 1000 hard cap
    * dry_run=true never touches Mongo
    * archive write is verified before hot-row delete
    * scheduler ON by default (INTENT_SWEEPER_ENABLED=true)

All tests scope their Mongo touch to a test-only intent_id prefix
via `_test_intent_id_prefix` — production rows in the shared
`test_database` are never affected.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "/app/backend")

from db import db  # noqa: E402
from namespaces import (  # noqa: E402
    CAPITAL_LEDGER,
    SHARED_INTENTS,
    SHARED_INTENTS_ARCHIVE,
)
from shared import intent_sweeper  # noqa: E402
from shared.intent_sweeper import learning_capture_required  # noqa: E402
from shared.learning.live_loop import LEARNING_EXPERIENCES  # noqa: E402


_PFX = "sweeper-test-"


def _iso(dt):
    return dt.isoformat()


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
async def _purge_test_rows():
    """Purge synthetic rows from all touched collections before + after."""
    async def _wipe():
        await db[SHARED_INTENTS].delete_many(
            {"intent_id": {"$regex": f"^{_PFX}"}},
        )
        await db[SHARED_INTENTS_ARCHIVE].delete_many(
            {"intent_id": {"$regex": f"^{_PFX}"}},
        )
        await db[LEARNING_EXPERIENCES].delete_many(
            {"intent_id": {"$regex": f"^{_PFX}"}},
        )
        await db[CAPITAL_LEDGER].update_many(
            {"reservations.intent_id": {"$regex": f"^{_PFX}"}},
            {"$pull": {"reservations": {"intent_id": {"$regex": f"^{_PFX}"}}}},
        )
    await _wipe()
    yield
    await _wipe()


async def _seed_intent(
    intent_id: str,
    *,
    hours_ago: float = 7.0,
    executed=None,
    broker_order_id=None,
    gate_state: str = "no_trade",
    action: str = "no_trade",
    symbol: str = "AAPL",
    lane: str = "equity",
) -> None:
    doc = {
        "intent_id": intent_id,
        "symbol": symbol,
        "lane": lane,
        "action": action,
        "ingest_ts": _iso(_now() - timedelta(hours=hours_ago)),
        "gate_state": gate_state,
    }
    if executed is not None:
        doc["executed"] = executed
    if broker_order_id is not None:
        doc["broker_order_id"] = broker_order_id
    await db[SHARED_INTENTS].insert_one(doc)


async def _seed_resolved_experience(intent_id: str) -> None:
    await db[LEARNING_EXPERIENCES].insert_one({
        "intent_id": intent_id,
        "symbol": "AAPL", "lane": "equity", "action": "BUY",
        "created_at": _iso(_now() - timedelta(hours=8)),
        "outcome_5m_bps": 42.5,
    })


async def _seed_unresolved_experience(intent_id: str) -> None:
    await db[LEARNING_EXPERIENCES].insert_one({
        "intent_id": intent_id,
        "outcome_5m_bps": None,
        "outcome_15m_bps": None,
        "outcome_1h_bps": None,
    })


async def _seed_active_reservation(intent_id: str, lane: str = "equity") -> None:
    doc_id = f"lane_ledger_{lane}"
    await db[CAPITAL_LEDGER].update_one(
        {"_id": doc_id},
        {
            "$setOnInsert": {"_id": doc_id, "lane": lane},
            "$push": {"reservations": {
                "intent_id": intent_id,
                "amount": 5.0,
                "status": "open",
                "reserved_at": _iso(_now()),
            }},
        },
        upsert=True,
    )


def _sweep(**kwargs):
    return intent_sweeper.sweep_stale_intents(
        db, _test_intent_id_prefix=_PFX, **kwargs,
    )


# ═══════════════════════════════════════════════════════════════════
#  Scheduler doctrine — ON by default
# ═══════════════════════════════════════════════════════════════════


def test_sweeper_scheduler_defaults_to_enabled():
    import os
    env_val = os.environ.get("INTENT_SWEEPER_ENABLED", "true").lower()
    assert env_val == "true"
    assert intent_sweeper.SWEEPER_ENABLED is True


# ═══════════════════════════════════════════════════════════════════
#  learning_capture_required() classifier (operator directive)
# ═══════════════════════════════════════════════════════════════════


def test_classifier_hold_row_does_not_require_capture():
    """A HOLD row was never supposed to enter the learning tape."""
    assert not learning_capture_required({
        "action": "HOLD", "gate_state": "no_trade",
    })


def test_classifier_no_trade_row_does_not_require_capture():
    assert not learning_capture_required({
        "action": "no_trade", "gate_state": "no_trade",
    })


def test_classifier_directional_blocked_pre_broker_does_not_require_capture():
    """A BUY blocked upstream of the broker never reached execution
    — no learning row was ever going to be created."""
    assert not learning_capture_required({
        "action": "BUY", "gate_state": "blocked",
    })


def test_classifier_directional_reached_broker_requires_capture():
    """A BUY that reached the broker (has an order id) MUST have a
    learning row — that's what the learning tape exists for."""
    assert learning_capture_required({
        "action": "BUY", "gate_state": "submitted",
        "broker_order_id": "abc-123",
    })


def test_classifier_broker_rejected_requires_capture():
    """A rejected order still reached the broker — the rejection is
    itself learning signal."""
    assert learning_capture_required({
        "action": "SELL", "gate_state": "broker_rejected",
    })


def test_classifier_reads_execution_action_when_top_level_missing():
    """Some emitters put `action` under `execution` — classifier must
    pick it up either way."""
    assert learning_capture_required({
        "execution": {"action": "SHORT"},
        "gate_state": "submitted",
    })


# ═══════════════════════════════════════════════════════════════════
#  Age gate
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_intent_younger_than_6h_is_preserved():
    await _seed_intent(f"{_PFX}young", hours_ago=5.5)
    counts = await _sweep(dry_run=False)
    assert counts["matched"] == 0
    assert await db[SHARED_INTENTS].find_one(
        {"intent_id": f"{_PFX}young"}
    ) is not None


@pytest.mark.asyncio
async def test_intent_older_than_6h_is_swept():
    await _seed_intent(f"{_PFX}old", hours_ago=7.0)
    counts = await _sweep(dry_run=True)
    assert counts["matched"] >= 1


# ═══════════════════════════════════════════════════════════════════
#  Preserve — query-level filters (executed / broker_order_id / submitted)
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_executed_true_intent_is_preserved():
    await _seed_intent(
        f"{_PFX}executed", hours_ago=48.0,
        executed=True, gate_state="submitted", action="BUY",
    )
    counts = await _sweep(dry_run=False)
    assert counts["matched"] == 0
    assert await db[SHARED_INTENTS].find_one(
        {"intent_id": f"{_PFX}executed"}
    ) is not None


@pytest.mark.asyncio
async def test_intent_with_broker_order_id_is_preserved():
    await _seed_intent(
        f"{_PFX}broker", hours_ago=48.0,
        broker_order_id="WEBULL-ORDER-12345", action="BUY",
    )
    counts = await _sweep(dry_run=False)
    assert counts["matched"] == 0


@pytest.mark.asyncio
async def test_submitted_gate_state_is_preserved():
    await _seed_intent(
        f"{_PFX}submitted", hours_ago=48.0,
        gate_state="submitted", action="BUY",
    )
    counts = await _sweep(dry_run=False)
    assert counts["matched"] == 0


# ═══════════════════════════════════════════════════════════════════
#  Preserve — active capital-ledger reservation
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_active_capital_reservation_preserves_intent():
    intent_id = f"{_PFX}res-open"
    await _seed_intent(intent_id, hours_ago=7, action="BUY", gate_state="blocked")
    await _seed_active_reservation(intent_id)

    counts = await _sweep(dry_run=False)
    assert counts["preserved_active_reservation"] >= 1
    assert counts["archived"] == 0
    assert await db[SHARED_INTENTS].find_one(
        {"intent_id": intent_id}
    ) is not None


# ═══════════════════════════════════════════════════════════════════
#  CATEGORICALLY: no_trade / HOLD / WATCH — no learning check
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_no_trade_row_without_learning_is_archived(monkeypatch):
    """The 2026-02-19 refinement — a no_trade row must NOT be
    preserved on 'missing learning'. It was never eligible for the
    learning tape. It should be archived with the
    `legacy_non_learning_no_trade` reason."""
    monkeypatch.setattr(intent_sweeper, "LEARNING_LOOP_ENABLED", True)
    intent_id = f"{_PFX}no-trade-no-learn"
    await _seed_intent(
        intent_id, hours_ago=7,
        action="no_trade", gate_state="no_trade",
    )

    counts = await _sweep(dry_run=False)
    assert counts["preserved_missing_learning"] == 0, (
        "No-trade rows must not trigger the missing-learning preserve"
    )
    assert counts["archived"] >= 1
    assert counts["learning_not_applicable"] >= 1

    archived = await db[SHARED_INTENTS_ARCHIVE].find_one(
        {"intent_id": intent_id},
    )
    assert archived is not None
    assert archived["archive_reason"] == "legacy_non_learning_no_trade"


@pytest.mark.asyncio
async def test_hold_action_is_archived_without_learning_row(monkeypatch):
    monkeypatch.setattr(intent_sweeper, "LEARNING_LOOP_ENABLED", True)
    intent_id = f"{_PFX}hold"
    await _seed_intent(
        intent_id, hours_ago=7,
        action="HOLD", gate_state="advisory_only",
    )
    counts = await _sweep(dry_run=False)
    assert counts["preserved_missing_learning"] == 0
    assert counts["archived"] >= 1


# ═══════════════════════════════════════════════════════════════════
#  Directional blocked pre-broker — archive w/ counterfactual reason
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_directional_blocked_pre_broker_archives_with_typed_reason(monkeypatch):
    """A BUY blocked upstream of broker → archive_reason should be
    `directional_blocked_pre_broker` so operator can filter for
    counterfactual/missed-trade analysis."""
    monkeypatch.setattr(intent_sweeper, "LEARNING_LOOP_ENABLED", True)
    intent_id = f"{_PFX}dir-blocked"
    await _seed_intent(
        intent_id, hours_ago=7,
        action="BUY", gate_state="blocked",
    )
    counts = await _sweep(dry_run=False)
    # Not learning-eligible (didn't reach broker) → not preserved.
    assert counts["preserved_missing_learning"] == 0
    assert counts["archived"] >= 1
    assert counts["learning_not_applicable"] >= 1  # per classifier

    archived = await db[SHARED_INTENTS_ARCHIVE].find_one(
        {"intent_id": intent_id},
    )
    assert archived["archive_reason"] == "directional_blocked_pre_broker"


# ═══════════════════════════════════════════════════════════════════
#  Preserve — learning REQUIRED but missing
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_directional_broker_rejected_without_learning_preserves(monkeypatch):
    """A broker-rejected BUY that has no learning row is a REAL
    learning gap — preserve until the learning loop catches up."""
    monkeypatch.setattr(intent_sweeper, "LEARNING_LOOP_ENABLED", True)
    intent_id = f"{_PFX}rej-no-learn"
    await _seed_intent(
        intent_id, hours_ago=7,
        action="BUY", gate_state="broker_rejected",
    )
    counts = await _sweep(dry_run=False)
    assert counts["preserved_missing_learning"] >= 1
    assert counts["archived"] == 0
    assert counts["learning_required"] >= 1
    assert await db[SHARED_INTENTS].find_one(
        {"intent_id": intent_id}
    ) is not None


@pytest.mark.asyncio
async def test_directional_reached_broker_with_learning_is_distilled(monkeypatch):
    """The happy path — a directional row that reached the broker
    AND has a resolved learning row → delete outright."""
    monkeypatch.setattr(intent_sweeper, "LEARNING_LOOP_ENABLED", True)
    intent_id = f"{_PFX}happy-distilled"
    # Note: gate_state=broker_rejected keeps the row in the sweep
    # candidate set (submitted would be preserved by query filter).
    await _seed_intent(
        intent_id, hours_ago=7,
        action="SELL", gate_state="broker_rejected",
    )
    await _seed_resolved_experience(intent_id)
    counts = await _sweep(dry_run=False)
    assert counts["deleted_distilled"] >= 1
    assert counts["archived"] == 0


# ═══════════════════════════════════════════════════════════════════
#  Dry-run + count semantics
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dry_run_never_touches_mongo(monkeypatch):
    monkeypatch.setattr(intent_sweeper, "LEARNING_LOOP_ENABLED", True)
    await _seed_intent(
        f"{_PFX}dry1", hours_ago=8, action="no_trade", gate_state="no_trade",
    )
    counts = await _sweep(dry_run=True)
    assert counts["dry_run"] is True
    assert counts["matched"] >= 1
    assert counts["eligible_for_purge"] >= 1
    # Mongo untouched.
    assert await db[SHARED_INTENTS].find_one(
        {"intent_id": f"{_PFX}dry1"}
    ) is not None
    assert await db[SHARED_INTENTS_ARCHIVE].find_one(
        {"intent_id": f"{_PFX}dry1"}
    ) is None


@pytest.mark.asyncio
async def test_counts_split_learning_required_vs_not_applicable(monkeypatch):
    """Counts must accurately reflect the classifier decision so the
    operator can tell "broken learning" from "normal non-trade"."""
    monkeypatch.setattr(intent_sweeper, "LEARNING_LOOP_ENABLED", True)
    # Two no_trade rows — classifier says "not applicable".
    await _seed_intent(f"{_PFX}nt-a", hours_ago=8, action="no_trade", gate_state="no_trade")
    await _seed_intent(f"{_PFX}nt-b", hours_ago=8, action="HOLD", gate_state="advisory_only")
    # One broker-rejected BUY — classifier says "required".
    await _seed_intent(f"{_PFX}req-a", hours_ago=8, action="BUY", gate_state="broker_rejected")

    counts = await _sweep(dry_run=True)
    assert counts["learning_not_applicable"] >= 2
    assert counts["learning_required"] >= 1


# ═══════════════════════════════════════════════════════════════════
#  Archive doc shape + verified-write flow
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_archive_stamps_all_four_doctrine_fields(monkeypatch):
    monkeypatch.setattr(intent_sweeper, "LEARNING_LOOP_ENABLED", False)
    intent_id = f"{_PFX}archive-1"
    await _seed_intent(
        intent_id, hours_ago=7,
        action="no_trade", gate_state="no_trade",
    )
    counts = await _sweep(dry_run=False)
    assert counts["archived"] >= 1

    archived = await db[SHARED_INTENTS_ARCHIVE].find_one(
        {"intent_id": intent_id},
    )
    assert archived["archive_version"] == "v1"
    assert archived["archive_reason"] in {
        "legacy_non_learning_no_trade",
        "directional_blocked_pre_broker",
        "stale_never_reached_broker",
    }
    assert archived["original_gate_state"] == "no_trade"
    assert archived["archived_at"]
    assert archived["symbol"] == "AAPL"

    assert await db[SHARED_INTENTS].find_one(
        {"intent_id": intent_id}
    ) is None


# ═══════════════════════════════════════════════════════════════════
#  Batch limits
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_batch_limit_caps_processing():
    for i in range(6):
        await _seed_intent(
            f"{_PFX}batch-{i}", hours_ago=7,
            action="no_trade", gate_state="no_trade",
        )
    counts = await _sweep(dry_run=True, batch_limit=3)
    assert counts["matched"] == 3
    assert counts["batch_limit"] == 3


@pytest.mark.asyncio
async def test_batch_limit_max_is_capped_at_1000():
    counts = await _sweep(dry_run=True, batch_limit=10_000)
    assert counts["batch_limit"] == 1000
