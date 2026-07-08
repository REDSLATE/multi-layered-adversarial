"""Tests for `shared/capital/ledger.py` — per-lane capital cap ledger.

Doctrine invariants:
    * `reserve_capital` is atomic — the CAS filter prevents two
      concurrent callers from both winning when only one has room.
    * `release_capital` is idempotent — safe to retry from broker
      reconcile without double-releasing.
    * `sweep_stale_reservations` frees only OPEN reservations older
      than the cutoff — never touches already-released or fresh ones.
    * `get_lane_headroom` never mutates.
    * Only two valid lanes: `equity` and `crypto`. Invalid lane
      raises ValueError.
"""
from __future__ import annotations

import asyncio
import sys
import uuid

import pytest

sys.path.insert(0, "/app/backend")

from db import db
from namespaces import CAPITAL_LEDGER
from shared.capital.ledger import (
    get_all_headroom,
    get_lane_headroom,
    get_open_reservations,
    init_ledger,
    release_capital,
    reserve_capital,
    sweep_stale_reservations,
)


@pytest.fixture(autouse=True)
async def _clean_ledger():
    """Wipe the ledger between tests so each starts from a known state."""
    await db[CAPITAL_LEDGER].delete_many({})
    yield
    await db[CAPITAL_LEDGER].delete_many({})


# ─────────────────────── init_ledger ───────────────────────


@pytest.mark.asyncio
async def test_init_creates_both_lane_docs():
    await init_ledger(equity_cap=1000.0, crypto_cap=500.0)
    equity = await get_lane_headroom("equity")
    crypto = await get_lane_headroom("crypto")
    assert equity["total"] == 1000.0
    assert equity["reserved"] == 0.0
    assert equity["available"] == 1000.0
    assert crypto["total"] == 500.0
    assert crypto["reserved"] == 0.0


@pytest.mark.asyncio
async def test_init_is_idempotent_and_preserves_reserved():
    await init_ledger(equity_cap=1000.0, crypto_cap=500.0)
    ok = await reserve_capital("equity", 100.0, "intent-1")
    assert ok is True
    # Re-init with a new cap; reserved must survive.
    await init_ledger(equity_cap=2000.0, crypto_cap=500.0)
    equity = await get_lane_headroom("equity")
    assert equity["total"] == 2000.0     # refreshed
    assert equity["reserved"] == 100.0   # preserved


# ─────────────────────── reserve_capital ───────────────────────


@pytest.mark.asyncio
async def test_reserve_below_cap_succeeds():
    await init_ledger(1000.0, 500.0)
    ok = await reserve_capital("equity", 400.0, "intent-1")
    assert ok is True
    head = await get_lane_headroom("equity")
    assert head["reserved"] == 400.0
    assert head["available"] == 600.0


@pytest.mark.asyncio
async def test_reserve_exceeding_cap_rejected_and_reserved_unchanged():
    await init_ledger(1000.0, 500.0)
    await reserve_capital("equity", 900.0, "intent-1")
    ok = await reserve_capital("equity", 200.0, "intent-2")
    assert ok is False
    head = await get_lane_headroom("equity")
    assert head["reserved"] == 900.0  # rejected reserve is a no-op
    assert head["available"] == 100.0


@pytest.mark.asyncio
async def test_reserve_at_exact_cap_boundary():
    await init_ledger(1000.0, 500.0)
    ok = await reserve_capital("equity", 1000.0, "intent-boundary")
    assert ok is True
    head = await get_lane_headroom("equity")
    assert head["available"] == 0.0
    # One more penny → reject
    ok = await reserve_capital("equity", 0.01, "intent-overflow")
    assert ok is False


@pytest.mark.asyncio
async def test_reserve_non_positive_amount_refused():
    await init_ledger(1000.0, 500.0)
    assert await reserve_capital("equity", 0.0, "intent-zero") is False
    assert await reserve_capital("equity", -10.0, "intent-neg") is False
    head = await get_lane_headroom("equity")
    assert head["reserved"] == 0.0


@pytest.mark.asyncio
async def test_reserve_without_init_returns_false():
    # No init_ledger call → doc doesn't exist.
    ok = await reserve_capital("equity", 100.0, "intent-1")
    assert ok is False


@pytest.mark.asyncio
async def test_reserve_isolates_lanes():
    """Reserving on equity does NOT affect crypto headroom."""
    await init_ledger(1000.0, 500.0)
    await reserve_capital("equity", 900.0, "eq-1")
    crypto = await get_lane_headroom("crypto")
    assert crypto["reserved"] == 0.0
    assert crypto["available"] == 500.0


@pytest.mark.asyncio
async def test_concurrent_reserves_at_boundary_only_one_wins():
    """Two concurrent reservations that together would exceed cap —
    the CAS filter must ensure exactly one wins."""
    await init_ledger(1000.0, 500.0)
    # Cap $1000; both attempts want $600 → exactly one can fit.
    task_a = asyncio.create_task(
        reserve_capital("equity", 600.0, "intent-a"),
    )
    task_b = asyncio.create_task(
        reserve_capital("equity", 600.0, "intent-b"),
    )
    results = await asyncio.gather(task_a, task_b)
    assert sum(results) == 1, (
        f"exactly one concurrent reserve must succeed, got results={results}"
    )
    head = await get_lane_headroom("equity")
    assert head["reserved"] == 600.0  # only one $600 reserve landed


# ─────────────── retry idempotency (2026-02-20 operator-flagged) ───────────────


@pytest.mark.asyncio
async def test_reserve_is_idempotent_on_same_intent_id():
    """The retry doctrine: `_route_one` on a transient broker error
    leaves the intent eligible for a next-tick retry WITHOUT
    releasing the reservation. When the retry re-enters, calling
    `reserve_capital` again with the SAME intent_id MUST be a
    no-op — do NOT double-charge the ledger."""
    await init_ledger(1000.0, 500.0)
    intent_id = "intent-retry-1"
    ok1 = await reserve_capital("equity", 300.0, intent_id)
    ok2 = await reserve_capital("equity", 300.0, intent_id)
    assert ok1 is True
    assert ok2 is True  # idempotent — returns True as no-op
    head = await get_lane_headroom("equity")
    # Only ONE $300 reservation actually landed on `reserved`.
    assert head["reserved"] == 300.0, (
        f"retry MUST NOT double-charge: reserved={head['reserved']}"
    )
    open_res = await get_open_reservations("equity")
    same_intent = [r for r in open_res if r["intent_id"] == intent_id]
    assert len(same_intent) == 1, (
        f"expected exactly one open reservation for {intent_id}, "
        f"got {len(same_intent)}"
    )


@pytest.mark.asyncio
async def test_reserve_idempotent_survives_multiple_retries():
    """Simulate the full retry loop: 5 successive reserves for the
    same intent (as would happen if a transient error repeats 4
    times before a terminal classification). Only the first should
    land; the other 4 must be idempotent no-ops."""
    await init_ledger(1000.0, 500.0)
    intent_id = "intent-retry-loop"
    results = []
    for _ in range(5):
        results.append(await reserve_capital("equity", 100.0, intent_id))
    assert all(results), (
        "all 5 retry-reserves must return True (first books, rest "
        f"idempotent no-op), got {results}"
    )
    head = await get_lane_headroom("equity")
    assert head["reserved"] == 100.0, (
        f"only one $100 should have landed, got reserved={head['reserved']}"
    )


@pytest.mark.asyncio
async def test_reserve_after_release_creates_new_reservation():
    """After release, the intent_id has NO open reservation → a fresh
    reserve for the same intent_id must fire cleanly (not treated as
    idempotent no-op)."""
    await init_ledger(1000.0, 500.0)
    intent_id = "intent-cycle"
    await reserve_capital("equity", 200.0, intent_id)
    await release_capital("equity", intent_id, 200.0, "position_closed")
    # A brand-new reserve on the SAME intent_id — this is a fresh
    # execution cycle from the caller's POV. Ledger must land it.
    ok = await reserve_capital("equity", 200.0, intent_id)
    assert ok is True
    head = await get_lane_headroom("equity")
    assert head["reserved"] == 200.0


@pytest.mark.asyncio
async def test_reserve_idempotent_wins_before_cap_exceeded_check():
    """Idempotent no-op must win even when the cap is already
    exceeded — because the intent's capital IS already accounted
    for in `reserved`. A retry should never surface as
    REJECTED_CAP_EXCEEDED when the reservation is already held."""
    await init_ledger(1000.0, 500.0)
    # Reserve $900 under intent-a, then fill the rest with intent-b.
    await reserve_capital("equity", 900.0, "intent-a")
    await reserve_capital("equity", 100.0, "intent-b")  # cap reached
    # Now intent-a retries — reservation IS already open, so this
    # must return True (idempotent), NOT False (cap exceeded).
    ok = await reserve_capital("equity", 900.0, "intent-a")
    assert ok is True, (
        "retry-of-already-held reservation must return True even "
        "at full-cap — otherwise the retry lands as a spurious "
        "REJECTED_CAP_EXCEEDED"
    )


@pytest.mark.asyncio
async def test_concurrent_reserve_same_intent_id_no_double_charge():
    """Two concurrent reserves for the SAME intent_id (racing retry
    reads at the exact same tick). CAS filter must ensure exactly
    one $inc lands, and both callers see success (True) via the
    idempotent path for the loser."""
    await init_ledger(1000.0, 500.0)
    intent_id = "intent-race"
    task_a = asyncio.create_task(
        reserve_capital("equity", 400.0, intent_id),
    )
    task_b = asyncio.create_task(
        reserve_capital("equity", 400.0, intent_id),
    )
    results = await asyncio.gather(task_a, task_b)
    # Both return True — one via first-write, one via idempotent no-op.
    assert results == [True, True], (
        f"both concurrent retries must return True, got {results}"
    )
    head = await get_lane_headroom("equity")
    # Exactly ONE $400 should have landed.
    assert head["reserved"] == 400.0, (
        f"concurrent same-intent MUST NOT double-charge: "
        f"reserved={head['reserved']}"
    )
    open_res = await get_open_reservations("equity")
    same = [r for r in open_res if r["intent_id"] == intent_id]
    assert len(same) == 1, (
        f"exactly one reservation record expected, got {len(same)}"
    )


@pytest.mark.asyncio
async def test_idempotency_is_scoped_per_lane():
    """Idempotency invariant: `intent_id` uniqueness is scoped
    PER LANE, not global. The CAS filter's `_id: <lane>_cap`
    restricts the check to a single lane doc.

    In practice, any given intent has exactly one lane so this
    doesn't fire — but the invariant matters for correctness. A
    same intent_id in equity + crypto is treated as two independent
    reservations (each lane's doc holds its own reservation array).
    """
    await init_ledger(1000.0, 500.0)
    same_id = "intent-shared-id"
    # First reserve on equity — succeeds cleanly.
    ok_eq = await reserve_capital("equity", 200.0, same_id)
    assert ok_eq is True
    # Second reserve on CRYPTO with the same intent_id — must
    # succeed as a fresh reservation (different lane doc, different
    # reservations array), NOT trip the idempotent no-op.
    ok_cr = await reserve_capital("crypto", 100.0, same_id)
    assert ok_cr is True

    eq = await get_lane_headroom("equity")
    cr = await get_lane_headroom("crypto")
    assert eq["reserved"] == 200.0, (
        f"equity reservation isolated, got reserved={eq['reserved']}"
    )
    assert cr["reserved"] == 100.0, (
        f"crypto reservation isolated, got reserved={cr['reserved']}"
    )
    # Now a THIRD reserve on equity with the same intent_id — this
    # IS the retry case, must be idempotent no-op.
    ok_eq2 = await reserve_capital("equity", 200.0, same_id)
    assert ok_eq2 is True
    eq_after = await get_lane_headroom("equity")
    assert eq_after["reserved"] == 200.0, (
        "same-lane same-intent retry MUST NOT double-charge "
        "even when the intent_id also exists in another lane"
    )


# ─────────────────────── release_capital ───────────────────────


@pytest.mark.asyncio
async def test_release_frees_reserved_amount():
    await init_ledger(1000.0, 500.0)
    await reserve_capital("equity", 300.0, "intent-1")
    ok = await release_capital("equity", "intent-1", 300.0, "position_closed")
    assert ok is True
    head = await get_lane_headroom("equity")
    assert head["reserved"] == 0.0
    assert head["available"] == 1000.0


@pytest.mark.asyncio
async def test_release_is_idempotent():
    await init_ledger(1000.0, 500.0)
    await reserve_capital("equity", 300.0, "intent-1")
    ok1 = await release_capital("equity", "intent-1", 300.0, "position_closed")
    ok2 = await release_capital("equity", "intent-1", 300.0, "position_closed")
    assert ok1 is True
    assert ok2 is False   # second call is a no-op — no open reservation
    head = await get_lane_headroom("equity")
    assert head["reserved"] == 0.0  # NOT double-decremented


@pytest.mark.asyncio
async def test_release_unknown_intent_id_no_op():
    """Release for an unknown intent must return False and NOT
    decrement `reserved` (safety — a stray release call cannot
    corrupt the counter)."""
    await init_ledger(1000.0, 500.0)
    await reserve_capital("equity", 500.0, "real-intent")
    ok = await release_capital(
        "equity", "unknown-intent", 100.0, "position_closed",
    )
    assert ok is False
    head = await get_lane_headroom("equity")
    assert head["reserved"] == 500.0   # unchanged


@pytest.mark.asyncio
async def test_release_records_reason_in_audit_trail():
    await init_ledger(1000.0, 500.0)
    await reserve_capital("equity", 100.0, "intent-1")
    await release_capital(
        "equity", "intent-1", 100.0, "broker_terminal_reject",
    )
    doc = await db[CAPITAL_LEDGER].find_one({"_id": "equity_cap"})
    releases = [r for r in doc["reservations"] if r["intent_id"] == "intent-1"]
    assert len(releases) == 1
    assert releases[0]["status"] == "released"
    assert releases[0]["release_reason"] == "broker_terminal_reject"
    assert releases[0]["released_at"] is not None


# ─────────────────────── sweep_stale_reservations ───────────────────────


@pytest.mark.asyncio
async def test_sweep_releases_only_stale_open_reservations():
    """Sweep with `max_age_minutes=0` must release EVERY open
    reservation (all are older than the immediate cutoff)."""
    await init_ledger(1000.0, 500.0)
    await reserve_capital("equity", 100.0, "intent-old-1")
    await reserve_capital("equity", 200.0, "intent-old-2")
    await asyncio.sleep(0.01)  # ensure timestamps < cutoff
    result = await sweep_stale_reservations("equity", max_age_minutes=0)
    assert result["released"] == 2
    assert result["amount_released"] == 300.0
    head = await get_lane_headroom("equity")
    assert head["reserved"] == 0.0


@pytest.mark.asyncio
async def test_sweep_skips_fresh_reservations():
    """Sweep with max_age_minutes=60 must NOT release fresh reservations."""
    await init_ledger(1000.0, 500.0)
    await reserve_capital("equity", 100.0, "intent-fresh")
    result = await sweep_stale_reservations("equity", max_age_minutes=60)
    assert result["released"] == 0
    head = await get_lane_headroom("equity")
    assert head["reserved"] == 100.0


@pytest.mark.asyncio
async def test_sweep_skips_already_released_reservations():
    """A previously-released reservation must not be swept again
    (which would incorrectly double-decrement `reserved`)."""
    await init_ledger(1000.0, 500.0)
    await reserve_capital("equity", 100.0, "intent-1")
    await release_capital("equity", "intent-1", 100.0, "position_closed")
    # Reserved is already 0; sweep must be a no-op.
    result = await sweep_stale_reservations("equity", max_age_minutes=0)
    assert result["released"] == 0
    head = await get_lane_headroom("equity")
    assert head["reserved"] == 0.0


# ─────────────────────── read paths ───────────────────────


@pytest.mark.asyncio
async def test_get_lane_headroom_returns_none_if_uninitialized():
    result = await get_lane_headroom("equity")
    assert result is None


@pytest.mark.asyncio
async def test_utilization_pct_computed_correctly():
    await init_ledger(1000.0, 500.0)
    await reserve_capital("equity", 250.0, "intent-1")
    head = await get_lane_headroom("equity")
    assert head["utilization_pct"] == 25.0


@pytest.mark.asyncio
async def test_get_open_reservations_returns_newest_first():
    await init_ledger(1000.0, 500.0)
    ids = [f"intent-{uuid.uuid4()}" for _ in range(3)]
    for iid in ids:
        await reserve_capital("equity", 100.0, iid)
        await asyncio.sleep(0.005)  # ensure distinct timestamps
    open_res = await get_open_reservations("equity", limit=10)
    assert len(open_res) == 3
    # Newest first — last-inserted intent_id should be first.
    assert open_res[0]["intent_id"] == ids[-1]
    assert open_res[-1]["intent_id"] == ids[0]


@pytest.mark.asyncio
async def test_get_open_reservations_excludes_released():
    await init_ledger(1000.0, 500.0)
    await reserve_capital("equity", 100.0, "intent-1")
    await reserve_capital("equity", 200.0, "intent-2")
    await release_capital("equity", "intent-1", 100.0, "position_closed")
    open_res = await get_open_reservations("equity")
    assert len(open_res) == 1
    assert open_res[0]["intent_id"] == "intent-2"


@pytest.mark.asyncio
async def test_get_all_headroom_returns_both_lanes():
    await init_ledger(1000.0, 500.0)
    result = await get_all_headroom()
    assert "equity" in result and "crypto" in result
    assert result["equity"]["total"] == 1000.0
    assert result["crypto"]["total"] == 500.0


# ─────────────────────── validation ───────────────────────


@pytest.mark.asyncio
async def test_invalid_lane_raises():
    with pytest.raises(ValueError):
        await reserve_capital("options", 100.0, "intent-1")
    with pytest.raises(ValueError):
        await release_capital("options", "intent-1", 100.0, "reason")
    with pytest.raises(ValueError):
        await get_lane_headroom("options")
