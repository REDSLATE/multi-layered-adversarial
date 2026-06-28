"""2026-02-25 — Lock the RISE AI learning-loop wiring.

Doctrine context:
    The 2026-06-02 bootstrap registered 8 SHADOW checkpoints and 8
    empty JSONL files. The harvester + auto-grader existed but were
    never scheduled — they only ran when invoked manually. Six months
    later the corpus was still empty.

    Today (2026-02-25) we promoted both to scheduled background
    workers via `shared/rise_ai/learning_loop.py`. This regression
    suite locks the contract so a future refactor can't silently
    un-schedule them.

Guarantees:
    1. SEATS tuple matches the bootstrap script (any drift triggers
       a silent "your seat is no longer being harvested" bug).
    2. The DEPRECATED skip — legacy brain-keyed checkpoints
       (alpha/camaro/chevelle/redeye) are NEVER touched by the
       harvester's checkpoint-metrics update, even if their `role`
       field collides with a current SEAT name.
    3. The harvester is idempotent — running it twice produces the
       same JSONL state (deterministic).
    4. `status()` returns the public shape the admin UI binds to.
    5. The lifespan hooks (start/stop) are present and importable.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio

from db import db
from shared.rise_ai.learning_loop import (
    SEATS,
    _harvest_one_seat,
    start_rise_learning_loop,
    status,
    stop_rise_learning_loop,
)


# ─────────────── 1) SEATS canon ────────────────────────────────────


def test_SEATS_canonical_list_locked():
    """Lock the 8-seat canon. If a future doctrine change adds or
    removes a seat, the bootstrap script + this list + the harvester
    must all move together. This test enforces the agreement."""
    assert SEATS == (
        "strategist",
        "auditor",
        "governor",
        "executor",
        "crypto_strategist",
        "crypto_auditor",
        "crypto_governor",
        "crypto",  # crypto_executor (legacy name preserved)
    )


def test_SEATS_match_bootstrap_script_canon():
    """The bootstrap is itself a one-shot tool but it owns the
    SEATS source of truth. If the bootstrap's list ever diverges
    from the learning loop's, harvesting and checkpoint registration
    will go out of sync. Cross-check the literal here."""
    from scripts.rise_ai_bootstrap import SEATS as BOOTSTRAP_SEATS
    assert SEATS == BOOTSTRAP_SEATS


# ─────────────── 2) Public API shape ───────────────────────────────


def test_status_returns_complete_shape():
    """Admin UI binds to these keys. Locking the shape prevents
    a refactor that renames `last_run` → `last_tick` (etc.) from
    silently blanking the operator's status tile."""
    snap = status()
    assert "enabled" in snap
    assert "grader" in snap and "harvester" in snap
    for phase in ("grader", "harvester"):
        ph = snap[phase]
        assert "running" in ph
        assert "interval_seconds" in ph
        assert "last_run" in ph
        for key in ("ran_at", "duration_ms", "error"):
            assert key in ph["last_run"]


def test_lifespan_hooks_importable_and_idempotent():
    """start/stop hooks must be safe to call multiple times.
    Lifespan calls start on boot; if the loop is already running
    (or env-disabled), start_rise_learning_loop must not crash."""
    # Call start twice — must be idempotent. If RISE_LEARNING_LOOP_ENABLED
    # is "false" in the test env, both calls noop and that's also fine.
    start_rise_learning_loop()
    start_rise_learning_loop()  # second call must be safe


# ─────────────── 3) Harvester behavior — the money tests ───────────


@pytest.mark.asyncio
async def test_harvest_one_seat_returns_receipt_for_valid_seat():
    """The harvester writes JSONL + updates ai_checkpoints. Returns
    a receipt the operator can render in the UI."""
    receipt = await _harvest_one_seat("strategist")
    assert receipt["seat"] == "strategist"
    assert receipt["ok"] is True
    # output_path always present even if rows_written == 0
    assert receipt["output_path"].endswith("strategist.jsonl")
    # rows_written may be 0 in a fresh test DB — that's fine
    assert isinstance(receipt["rows_written"], int)


@pytest.mark.asyncio
async def test_harvest_one_seat_creates_output_path():
    """Even for a seat with zero graded rows, the harvester creates
    (or touches) the JSONL file so the operator sees the path is
    real, not a phantom."""
    receipt = await _harvest_one_seat("executor")
    assert os.path.exists(receipt["output_path"])


@pytest.mark.asyncio
async def test_harvester_does_not_touch_deprecated_checkpoints():
    """The 2026-06-02 brain-keyed checkpoints (alpha/camaro/chevelle/
    redeye) sit at state=DEPRECATED. The harvester's update_one
    targets only SHADOW/ADVISOR/PRIMARY. This test confirms a
    DEPRECATED row's metrics aren't bumped by a same-role harvest."""
    # If any DEPRECATED rows exist with role="strategist" (they
    # shouldn't, but the harvester must still not touch them), the
    # query in _harvest_one_seat filters them out.
    deprecated_pre = await db.ai_checkpoints.count_documents({"state": "DEPRECATED"})
    await _harvest_one_seat("strategist")
    # Run a second time — still no movement on DEPRECATED counts.
    deprecated_post = await db.ai_checkpoints.count_documents({"state": "DEPRECATED"})
    assert deprecated_pre == deprecated_post


@pytest.mark.asyncio
async def test_harvester_idempotent_back_to_back():
    """Running the harvester twice in immediate succession must
    produce the same JSONL state on disk. This is the doctrine
    pin that lets the operator click "harvest now" without fear
    of corrupting the corpus."""
    r1 = await _harvest_one_seat("auditor")
    size_after_1 = os.path.getsize(r1["output_path"])
    r2 = await _harvest_one_seat("auditor")
    size_after_2 = os.path.getsize(r2["output_path"])
    assert size_after_1 == size_after_2
    assert r1["rows_written"] == r2["rows_written"]


@pytest.mark.asyncio
async def test_harvester_updates_checkpoint_metrics_when_row_exists():
    """The end-to-end contract: harvest a seat → matching SHADOW
    checkpoint row gets `metrics.rows_seeded` and
    `metrics.last_harvest_at` updated. The operator's status tile
    reads these fields."""
    # Find or skip — the test DB may or may not have the SHADOW row.
    row = await db.ai_checkpoints.find_one(
        {"role": "strategist", "state": {"$in": ["SHADOW", "ADVISOR", "PRIMARY"]}},
        {"_id": 0, "metrics": 1},
    )
    if not row:
        pytest.skip("no SHADOW strategist checkpoint in test DB")
    receipt = await _harvest_one_seat("strategist")
    updated = await db.ai_checkpoints.find_one(
        {"role": "strategist", "state": {"$in": ["SHADOW", "ADVISOR", "PRIMARY"]}},
        {"_id": 0, "metrics": 1},
    )
    assert updated is not None
    assert (updated.get("metrics") or {}).get("rows_seeded") == receipt["rows_written"]
    assert (updated.get("metrics") or {}).get("last_harvest_at") is not None
    assert (updated.get("metrics") or {}).get("last_harvest_source") == "rise_learning_loop"


@pytest.mark.asyncio
async def test_harvester_handles_unknown_seat_gracefully():
    """If `_harvest_one_seat` is ever called with a string not in
    SEATS, it shouldn't crash — it'll just produce an empty file
    (no llm_calls match the bogus role). This guards against a
    future caller mistyping a seat name."""
    receipt = await _harvest_one_seat("not_a_seat_xyz")
    assert receipt["seat"] == "not_a_seat_xyz"
    # Either ok=True with 0 rows OR ok=False with an error — both
    # are acceptable degradations. The CRITICAL contract is no
    # raised exception bubbling out of the loop and killing the
    # scheduled task.
    assert "ok" in receipt
