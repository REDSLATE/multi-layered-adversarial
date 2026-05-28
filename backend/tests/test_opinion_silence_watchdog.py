"""Opinion-silent watchdog — doctrine tripwires (2026-05-28).

Locks the watchdog's advisory-only behavior + correctness:

Doctrine pin (D-OPINION-SILENCE-2026-05-28):
  The watchdog SCANS occupied seats, computes opinion age vs threshold,
  and writes `opinion_silence_alerts` rows. It MUST NOT:
    - reassign a seat
    - mutate any seat policy
    - emit any may_execute=True flag
    - read or expose broker keys

Correctness pins:
  * The age field must be read from `posted_at` — the schema field
    actually written by `shared/opinions.py::post_opinion`. The
    earlier draft read `created_at` (a field nothing writes) and
    falsely flagged every brain as "never posted".
  * Vacant seats must be SKIPPED (no None-brain alerts).
  * `LIVE_RUNTIMES`-only — advisors / off-roster brains are not
    scanned (they have no opinion-posting obligation).
  * Cooldown must throttle repeat alerts for the same (brain, seat).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from db import db
from namespaces import LIVE_RUNTIMES, SHARED_OPINIONS
from routes.opinion_silence_watchdog import (
    ALERT_COOLDOWN_SEC,
    DEFAULT_SILENCE_THRESHOLD_SEC,
    _last_opinion_age,
    _recent_alert_exists,
    perform_scan,
)


pytestmark = [pytest.mark.tripwire]


# ──────────────────────── DOCTRINE INVARIANTS ────────────────────────


def test_default_threshold_matches_seat_roster_orange_band():
    """The default silence threshold must equal the Seat Roster UI's
    orange band (OPINION_FRESH_SEC * 4 = 4h). A watchdog alert about
    a seat the operator's UI still calls 'fresh' is a UX bug."""
    assert DEFAULT_SILENCE_THRESHOLD_SEC == 4 * 60 * 60


def test_default_cooldown_throttles_alert_writes():
    """Cooldown must be ≥ 5 minutes — otherwise a single silent seat
    floods `opinion_silence_alerts` with duplicates every tick."""
    assert ALERT_COOLDOWN_SEC >= 300


def test_module_has_no_execution_authority():
    """The watchdog module MUST NOT import any execution surface."""
    import inspect

    from routes import opinion_silence_watchdog as mod
    src = inspect.getsource(mod)
    # Hard bans — no broker/execution code paths in the watchdog.
    for forbidden in (
        "broker_router", "alpaca_credentials", "kraken_credentials",
        "may_execute = True", "execution.submit", "place_order",
    ):
        assert forbidden not in src, (
            f"DOCTRINE VIOLATION: watchdog references {forbidden!r}"
        )


def test_worker_module_has_no_execution_authority():
    """The background worker module MUST NOT import any execution surface."""
    import inspect

    from shared.runtime import opinion_silence_worker as mod
    src = inspect.getsource(mod)
    for forbidden in (
        "broker_router", "alpaca_credentials", "kraken_credentials",
        "may_execute = True", "execution.submit", "place_order",
    ):
        assert forbidden not in src, (
            f"DOCTRINE VIOLATION: worker references {forbidden!r}"
        )


# ──────────────────────── _last_opinion_age ────────────────────────


async def _wipe(brain: str) -> None:
    await db[SHARED_OPINIONS].delete_many({"runtime": brain, "_test_marker": True})
    await db["opinion_silence_alerts"].delete_many({
        "brain": brain, "_test_marker": True,
    })


async def _insert_opinion(brain: str, posted_at: datetime) -> str:
    oid = f"test-{uuid.uuid4()}"
    await db[SHARED_OPINIONS].insert_one({
        "opinion_id": oid,
        "runtime": brain,
        "topic": "symbol:TEST",
        "stance": "observation",
        "confidence": 0.5,
        "body": "tripwire fixture",
        "evidence": {},
        "in_reply_to": None,
        "thread_root": oid,
        "depth": 0,
        "may_execute": False,
        "posted_at": posted_at.isoformat(),
        "_test_marker": True,
    })
    return oid


async def test_last_opinion_age_reads_posted_at_field():
    """The watchdog MUST read `posted_at` (the schema field). Earlier
    drafts read `created_at`, which is never written — every brain
    looked 'never posted'. Uses a synthetic brain name so live data
    for real brains doesn't poison the age computation."""
    brain = "__test_silence_fixture__"
    await db[SHARED_OPINIONS].delete_many({"runtime": brain})
    posted = datetime.now(timezone.utc) - timedelta(seconds=120)
    await _insert_opinion(brain, posted)
    age = await _last_opinion_age(brain)
    assert age is not None, "watchdog must find a recent opinion"
    assert 110 <= age <= 200, f"expected age ~120s, got {age}"
    await db[SHARED_OPINIONS].delete_many({"runtime": brain})


async def test_last_opinion_age_none_when_never_posted():
    """A brain that has never posted returns None — distinguishable
    from 'posted long ago'."""
    brain = "__test_silence_neverposted__"
    await db[SHARED_OPINIONS].delete_many({"runtime": brain})
    age = await _last_opinion_age(brain)
    assert age is None


# ──────────────────────── perform_scan integration ────────────────────────


async def test_perform_scan_dry_run_never_writes_alerts():
    """dry_run=True must not insert any alert rows."""
    before = await db["opinion_silence_alerts"].count_documents({})
    result = await perform_scan(
        threshold_sec=60, cooldown_sec=60, dry_run=True,
    )
    after = await db["opinion_silence_alerts"].count_documents({})
    assert before == after, "dry_run wrote alerts — must not"
    assert result["dry_run"] is True
    assert result["doctrine"] == "advisory_observability_only"


async def test_perform_scan_skips_vacant_seats():
    """Vacant seat (brain=None) must produce no alerts."""
    # Force a clean roster: pick any vacant seat and assert no row
    # mentions a None brain.
    result = await perform_scan(
        threshold_sec=60, cooldown_sec=60, dry_run=True,
    )
    for row in result.get("flagged", []) + result.get("skipped_fresh", []):
        assert row.get("brain") is not None, (
            "vacant seat surfaced in scan output"
        )


async def test_perform_scan_only_scans_live_runtimes():
    """Only LIVE_RUNTIMES brains are eligible. Off-roster identities
    (advisors, system actors) are NOT silence-monitored."""
    result = await perform_scan(
        threshold_sec=60, cooldown_sec=60, dry_run=True,
    )
    for row in (
        result.get("flagged", [])
        + result.get("skipped_fresh", [])
        + result.get("skipped_cooldown", [])
    ):
        assert row["brain"] in LIVE_RUNTIMES, (
            f"non-runtime brain {row['brain']!r} surfaced in scan"
        )


async def test_perform_scan_writes_alert_for_stale_seat():
    """A seated brain with a stale-but-existing last opinion produces
    one alert row of kind='stale'."""
    # Find a brain currently seated in the live roster.
    from shared.roster import get_roster
    roster = await get_roster()
    assignments = (roster or {}).get("assignments") or {}
    seated_brain = next(
        (b for b in assignments.values() if b in LIVE_RUNTIMES), None,
    )
    if seated_brain is None:
        pytest.skip("no live runtime currently seated")

    # Plant a very old opinion so the brain looks stale.
    await _wipe(seated_brain)
    await _insert_opinion(
        seated_brain, datetime.now(timezone.utc) - timedelta(hours=24),
    )

    # Tight threshold (1s), tight cooldown (1s) so we always write.
    result = await perform_scan(
        threshold_sec=1, cooldown_sec=1, dry_run=False,
    )

    matching = [
        f for f in result.get("flagged", [])
        if f["brain"] == seated_brain
    ]
    assert matching, (
        f"stale seated brain {seated_brain!r} not flagged: {result}"
    )
    flagged = matching[0]
    assert flagged["kind"] == "stale"
    assert flagged["authority"] == "advisory_observability_only"
    assert flagged.get("threshold_sec") == 1

    # Verify it landed in mongo.
    written = await db["opinion_silence_alerts"].find_one(
        {"brain": seated_brain, "seat": flagged["seat"]},
        sort=[("ts_epoch", -1)],
    )
    assert written is not None

    await _wipe(seated_brain)
    await db["opinion_silence_alerts"].delete_many({"brain": seated_brain})


async def test_perform_scan_respects_cooldown():
    """Two scans inside the cooldown window MUST NOT double-write
    for the same (brain, seat)."""
    from shared.roster import get_roster
    roster = await get_roster()
    assignments = (roster or {}).get("assignments") or {}
    seated_brain = next(
        (b for b in assignments.values() if b in LIVE_RUNTIMES), None,
    )
    if seated_brain is None:
        pytest.skip("no live runtime currently seated")

    await _wipe(seated_brain)
    await db["opinion_silence_alerts"].delete_many({"brain": seated_brain})
    await _insert_opinion(
        seated_brain, datetime.now(timezone.utc) - timedelta(hours=24),
    )

    # First scan — should write.
    first = await perform_scan(
        threshold_sec=1, cooldown_sec=3600, dry_run=False,
    )
    first_count = sum(
        1 for r in first.get("flagged", []) if r["brain"] == seated_brain
    )
    assert first_count >= 1

    # Second scan inside cooldown — must skip.
    second = await perform_scan(
        threshold_sec=1, cooldown_sec=3600, dry_run=False,
    )
    second_count = sum(
        1 for r in second.get("flagged", []) if r["brain"] == seated_brain
    )
    skipped_cooldown = [
        r for r in second.get("skipped_cooldown", [])
        if r["brain"] == seated_brain
    ]
    assert second_count == 0, "cooldown failed — re-wrote alert"
    assert skipped_cooldown, "cooldown skip not reported"

    await _wipe(seated_brain)
    await db["opinion_silence_alerts"].delete_many({"brain": seated_brain})


async def test_recent_alert_exists_true_within_window():
    """Direct unit test for the cooldown helper."""
    brain = LIVE_RUNTIMES[0]
    seat = "__test_seat__"
    now_epoch = datetime.now(timezone.utc).timestamp()
    await db["opinion_silence_alerts"].insert_one({
        "brain": brain, "seat": seat,
        "ts_epoch": now_epoch,
        "_test_marker": True,
    })
    try:
        assert await _recent_alert_exists(brain, seat, 60) is True
        assert await _recent_alert_exists(brain, "__other_seat__", 60) is False
    finally:
        await db["opinion_silence_alerts"].delete_many(
            {"brain": brain, "seat": seat, "_test_marker": True},
        )


# ──────────────────────── worker lifecycle ────────────────────────


async def test_worker_start_stop_is_idempotent():
    """Starting twice must not spawn two tasks; stop must cancel cleanly."""
    from shared.runtime import opinion_silence_worker as mod
    # Force enabled, fast tick so the loop is alive but harmless.
    import os
    os.environ["OPINION_SILENCE_WATCHDOG_ENABLED"] = "true"
    os.environ["OPINION_SILENCE_WATCHDOG_TICK_SEC"] = "3600"
    try:
        mod.start_worker()
        task_a = mod._worker_task
        mod.start_worker()
        task_b = mod._worker_task
        assert task_a is task_b, "start_worker double-spawned"
        assert task_a is not None and not task_a.done()
        await mod.stop_worker()
        assert mod._worker_task is None
    finally:
        await mod.stop_worker()


async def test_worker_disabled_flag_skips_start():
    """OPINION_SILENCE_WATCHDOG_ENABLED=false must short-circuit."""
    from shared.runtime import opinion_silence_worker as mod
    import os
    await mod.stop_worker()
    os.environ["OPINION_SILENCE_WATCHDOG_ENABLED"] = "false"
    try:
        mod.start_worker()
        assert mod._worker_task is None, (
            "disabled flag ignored — worker still spawned"
        )
    finally:
        os.environ["OPINION_SILENCE_WATCHDOG_ENABLED"] = "true"
