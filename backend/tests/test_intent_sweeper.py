"""Stale-intent sweeper — archive-then-delete tests.

Locks the operator's 2026-02-19 doctrine:
    * 6-hour minimum age gate
    * Preserve executed / broker_order_id / submitted intents
    * Preserve intents with active capital-ledger reservations
    * Preserve intents when learning capture incomplete (learning
      loop enabled + no experience row yet)
    * Learning-aware bifurcation (distilled → delete outright,
      not-distilled → archive-then-delete)
    * Batch bounded at 1000 hard cap
    * dry_run=true never touches Mongo
    * archive doc stamps: archived_at / archive_reason /
      original_gate_state / archive_version="v1"
    * archive write is verified before hot-row delete
    * scheduler OFF by default (INTENT_SWEEPER_ENABLED=false)

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
        # Clean up test-created capital reservations too.
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
    gate_state: str = "blocked",
) -> None:
    doc = {
        "intent_id": intent_id,
        "symbol": "AAPL",
        "lane": "equity",
        "action": "BUY",
        "ingest_ts": _iso(_now() - timedelta(hours=hours_ago)),
        "gate_state": gate_state,
    }
    if executed is not None:
        doc["executed"] = executed
    if broker_order_id is not None:
        doc["broker_order_id"] = broker_order_id
    await db[SHARED_INTENTS].insert_one(doc)


async def _seed_resolved_experience(intent_id: str) -> None:
    """Insert a learning_experiences row with at least one horizon
    resolved — makes the intent eligible for outright delete."""
    await db[LEARNING_EXPERIENCES].insert_one({
        "intent_id": intent_id,
        "symbol": "AAPL", "lane": "equity", "action": "BUY",
        "created_at": _iso(_now() - timedelta(hours=8)),
        "outcome_5m_bps": 42.5,   # resolved
        "outcome_15m_bps": None,
        "outcome_1h_bps": None,
    })


async def _seed_unresolved_experience(intent_id: str) -> None:
    """Experience exists but no horizon resolved yet."""
    await db[LEARNING_EXPERIENCES].insert_one({
        "intent_id": intent_id,
        "outcome_5m_bps": None,
        "outcome_15m_bps": None,
        "outcome_1h_bps": None,
    })


async def _seed_active_reservation(intent_id: str, lane: str = "equity") -> None:
    """Attach an OPEN reservation for `intent_id` to the lane ledger doc."""
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
    """Wrapper that always applies the test prefix filter so we
    never touch prod rows."""
    return intent_sweeper.sweep_stale_intents(
        db, _test_intent_id_prefix=_PFX, **kwargs,
    )


# ═══════════════════════════════════════════════════════════════════
#  Scheduler doctrine — OFF by default (2026-02-19 operator directive)
# ═══════════════════════════════════════════════════════════════════


def test_sweeper_scheduler_defaults_to_enabled():
    """Operator directive (2026-02-19, revised): scheduler ON by
    default. Endpoint remains for manual dry-runs. Flip
    INTENT_SWEEPER_ENABLED=false to pause."""
    import os
    # SWEEPER_ENABLED is set at import time — default from env is
    # "true" per the doctrine pin.
    env_val = os.environ.get("INTENT_SWEEPER_ENABLED", "true").lower()
    assert env_val == "true"
    # The module-level flag must match.
    assert intent_sweeper.SWEEPER_ENABLED is True


# ═══════════════════════════════════════════════════════════════════
#  Age gate
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_intent_younger_than_6h_is_preserved():
    """A 5.5h-old intent is BELOW the age gate — must not be touched."""
    await _seed_intent(f"{_PFX}young", hours_ago=5.5, gate_state="blocked")
    counts = await _sweep(dry_run=False)
    assert counts["scanned"] == 0
    remaining = await db[SHARED_INTENTS].find_one({"intent_id": f"{_PFX}young"})
    assert remaining is not None


@pytest.mark.asyncio
async def test_intent_older_than_6h_is_swept():
    """A 7h-old intent CROSSES the age gate — must be candidate."""
    await _seed_intent(f"{_PFX}old", hours_ago=7.0, gate_state="blocked")
    counts = await _sweep(dry_run=True)
    assert counts["scanned"] >= 1


# ═══════════════════════════════════════════════════════════════════
#  Preserve-forever filters (query-level)
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_executed_true_intent_is_preserved():
    await _seed_intent(
        f"{_PFX}executed", hours_ago=48.0,
        executed=True, gate_state="submitted",
    )
    counts = await _sweep(dry_run=False)
    assert counts["scanned"] == 0
    assert await db[SHARED_INTENTS].find_one(
        {"intent_id": f"{_PFX}executed"}
    ) is not None


@pytest.mark.asyncio
async def test_intent_with_broker_order_id_is_preserved():
    await _seed_intent(
        f"{_PFX}broker", hours_ago=48.0,
        broker_order_id="WEBULL-ORDER-12345", gate_state="blocked",
    )
    counts = await _sweep(dry_run=False)
    assert counts["scanned"] == 0
    assert await db[SHARED_INTENTS].find_one(
        {"intent_id": f"{_PFX}broker"}
    ) is not None


@pytest.mark.asyncio
async def test_submitted_gate_state_is_preserved():
    await _seed_intent(
        f"{_PFX}submitted", hours_ago=48.0, gate_state="submitted",
    )
    counts = await _sweep(dry_run=False)
    assert counts["scanned"] == 0
    assert await db[SHARED_INTENTS].find_one(
        {"intent_id": f"{_PFX}submitted"}
    ) is not None


# ═══════════════════════════════════════════════════════════════════
#  Preserve — active capital-ledger reservation
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_active_capital_reservation_preserves_intent():
    """An intent with an OPEN reservation on the capital ledger is
    still in-flight from the reconciler's POV — do not touch."""
    intent_id = f"{_PFX}res-open"
    await _seed_intent(intent_id, hours_ago=7, gate_state="blocked")
    await _seed_active_reservation(intent_id, lane="equity")

    counts = await _sweep(dry_run=False)
    assert counts["preserved_active_reservation"] >= 1
    assert counts["archived"] == 0
    assert counts["deleted_distilled"] == 0
    # Hot row untouched.
    assert await db[SHARED_INTENTS].find_one(
        {"intent_id": intent_id}
    ) is not None


@pytest.mark.asyncio
async def test_released_reservation_does_not_preserve():
    """A `status=released` reservation is closed — the intent is
    fair game (subject to other rules)."""
    intent_id = f"{_PFX}res-released"
    await _seed_intent(intent_id, hours_ago=7, gate_state="blocked")
    await db[CAPITAL_LEDGER].update_one(
        {"_id": "lane_ledger_equity"},
        {
            "$setOnInsert": {"_id": "lane_ledger_equity", "lane": "equity"},
            "$push": {"reservations": {
                "intent_id": intent_id,
                "amount": 5.0,
                "status": "released",
                "released_at": _iso(_now()),
            }},
        },
        upsert=True,
    )

    counts = await _sweep(dry_run=False)
    assert counts["preserved_active_reservation"] == 0


# ═══════════════════════════════════════════════════════════════════
#  Preserve — learning capture incomplete (when learning enabled)
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_missing_learning_capture_preserves_intent(monkeypatch):
    """When learning is ENABLED and no experience row exists yet, the
    intent is preserved (learning may still be catching up)."""
    monkeypatch.setattr(intent_sweeper, "LEARNING_LOOP_ENABLED", True)
    intent_id = f"{_PFX}no-capture"
    await _seed_intent(intent_id, hours_ago=7, gate_state="blocked")
    # NO learning_experiences row inserted.

    counts = await _sweep(dry_run=False)
    assert counts["preserved_learning_capture_incomplete"] >= 1
    assert counts["archived"] == 0
    # Hot row untouched.
    assert await db[SHARED_INTENTS].find_one(
        {"intent_id": intent_id}
    ) is not None


@pytest.mark.asyncio
async def test_present_learning_capture_allows_archive(monkeypatch):
    """Learning enabled + unresolved experience row EXISTS → capture
    is complete → row is eligible for archive."""
    monkeypatch.setattr(intent_sweeper, "LEARNING_LOOP_ENABLED", True)
    intent_id = f"{_PFX}capture-ok"
    await _seed_intent(intent_id, hours_ago=7, gate_state="blocked")
    await _seed_unresolved_experience(intent_id)

    counts = await _sweep(dry_run=False)
    assert counts["archived"] >= 1
    assert counts["preserved_learning_capture_incomplete"] == 0


@pytest.mark.asyncio
async def test_learning_disabled_skips_capture_check(monkeypatch):
    """When learning is OFF the missing-experience preserve rule
    doesn't apply — archive proceeds normally."""
    monkeypatch.setattr(intent_sweeper, "LEARNING_LOOP_ENABLED", False)
    intent_id = f"{_PFX}learning-off"
    await _seed_intent(intent_id, hours_ago=7, gate_state="blocked")

    counts = await _sweep(dry_run=False)
    assert counts["archived"] >= 1
    assert counts["preserved_learning_capture_incomplete"] == 0


# ═══════════════════════════════════════════════════════════════════
#  Dry-run safety
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dry_run_never_touches_mongo(monkeypatch):
    monkeypatch.setattr(intent_sweeper, "LEARNING_LOOP_ENABLED", False)
    await _seed_intent(f"{_PFX}dry1", hours_ago=8, gate_state="blocked")
    await _seed_intent(f"{_PFX}dry2", hours_ago=8, gate_state="no_trade")
    counts = await _sweep(dry_run=True)
    assert counts["dry_run"] is True
    assert counts["scanned"] >= 2
    assert await db[SHARED_INTENTS].find_one(
        {"intent_id": f"{_PFX}dry1"}
    ) is not None
    assert await db[SHARED_INTENTS].find_one(
        {"intent_id": f"{_PFX}dry2"}
    ) is not None
    n_archive = await db[SHARED_INTENTS_ARCHIVE].count_documents({
        "intent_id": {"$in": [f"{_PFX}dry1", f"{_PFX}dry2"]},
    })
    assert n_archive == 0


# ═══════════════════════════════════════════════════════════════════
#  Archive-then-delete path
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_archive_then_delete_writes_full_doc_and_stamps(monkeypatch):
    monkeypatch.setattr(intent_sweeper, "LEARNING_LOOP_ENABLED", False)
    intent_id = f"{_PFX}archive-1"
    await _seed_intent(intent_id, hours_ago=7, gate_state="blocked")
    counts = await _sweep(dry_run=False)
    assert counts["archived"] >= 1
    assert counts["deleted_after_archive"] >= 1

    archived = await db[SHARED_INTENTS_ARCHIVE].find_one(
        {"intent_id": intent_id},
    )
    assert archived is not None
    assert archived["archive_version"] == "v1"
    assert archived["archive_reason"] == "stale_never_reached_broker"
    assert archived["original_gate_state"] == "blocked"
    assert archived["archived_at"]
    assert archived["symbol"] == "AAPL"

    assert await db[SHARED_INTENTS].find_one(
        {"intent_id": intent_id}
    ) is None


# ═══════════════════════════════════════════════════════════════════
#  Learning-aware bifurcation — distilled → delete outright
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_distilled_intent_is_deleted_without_archive(monkeypatch):
    monkeypatch.setattr(intent_sweeper, "LEARNING_LOOP_ENABLED", True)
    intent_id = f"{_PFX}distilled"
    await _seed_intent(intent_id, hours_ago=7, gate_state="blocked")
    await _seed_resolved_experience(intent_id)

    counts = await _sweep(dry_run=False)
    assert counts["deleted_distilled"] >= 1
    assert counts["archived"] == 0

    assert await db[SHARED_INTENTS].find_one(
        {"intent_id": intent_id}
    ) is None
    archived = await db[SHARED_INTENTS_ARCHIVE].find_one(
        {"intent_id": intent_id},
    )
    assert archived is None


# ═══════════════════════════════════════════════════════════════════
#  Batch limits
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_batch_limit_caps_processing():
    for i in range(6):
        await _seed_intent(
            f"{_PFX}batch-{i}", hours_ago=7, gate_state="blocked",
        )
    counts = await _sweep(dry_run=True, batch_limit=3)
    assert counts["scanned"] == 3
    assert counts["batch_limit"] == 3


@pytest.mark.asyncio
async def test_batch_limit_max_is_capped_at_1000():
    counts = await _sweep(dry_run=True, batch_limit=10_000)
    assert counts["batch_limit"] == 1000
