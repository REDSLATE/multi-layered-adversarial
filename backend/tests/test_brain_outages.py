"""Brain outage history contract pin (2026-02-20).

Doctrine: derive outage events from existing `sidecar_checkin_audit`
gaps. No new collection, no new writes. Read-only doctrine.

What this pins:
  1. Outage detected when consecutive check-ins are >= min_gap_sec apart.
  2. "Currently down" detected when latest check-in is >= min_gap_sec old.
  3. No false positives: dense check-ins (every minute) → no outages.
  4. Endpoint shape stable (per_brain, fleet_summary, doctrine fields).
  5. Source tripwire: router wired into server.py.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.tripwire
def test_brain_outages_router_is_wired():
    with open("/app/backend/server.py") as f:
        src = f.read()
    assert "brain_outages_router" in src, (
        "brain-outages admin route is not included in server.py — "
        "operators have no way to see recurrence patterns"
    )


@pytest.mark.asyncio
async def test_outage_detection_finds_gaps():
    """Three audit rows separated by a 1h gap should produce one
    outage event with duration ~3600s."""
    from db import db
    from routes.brain_outages import _outages_for_brain

    brain = "alpha"
    # Clean slate.
    await db["sidecar_checkin_audit"].delete_many({"runtime": brain})

    now = datetime.now(timezone.utc)
    rows = [
        # Two clustered check-ins ~1 min apart (no gap between them)
        {"runtime": brain, "ts": (now - timedelta(hours=3)).isoformat()},
        {"runtime": brain, "ts": (now - timedelta(hours=3, minutes=-1)).isoformat()},
        # 3h gap here — outage event #1 (the only one)
        {"runtime": brain, "ts": (now - timedelta(seconds=30)).isoformat()},
    ]
    await db["sidecar_checkin_audit"].insert_many(rows)

    events = await _outages_for_brain(
        brain, since=now - timedelta(hours=24), min_gap_sec=300,
    )
    assert len(events) == 1, f"expected 1 outage, got {events}"
    assert events[0]["recovered"] is True
    # ~3h gap = 10_740s (from 2h59m ago to 30s ago)
    assert events[0]["duration_sec"] > 10_000
    assert events[0]["duration_sec"] < 12_000

    await db["sidecar_checkin_audit"].delete_many({"runtime": brain})


@pytest.mark.asyncio
async def test_outage_detection_currently_down():
    """If the latest check-in is older than min_gap_sec, the brain
    is currently down and the last event has `recovered: false`."""
    from db import db
    from routes.brain_outages import _outages_for_brain

    brain = "alpha"
    await db["sidecar_checkin_audit"].delete_many({"runtime": brain})

    now = datetime.now(timezone.utc)
    # Single check-in from 2h ago, nothing since.
    await db["sidecar_checkin_audit"].insert_one(
        {"runtime": brain, "ts": (now - timedelta(hours=2)).isoformat()},
    )

    events = await _outages_for_brain(
        brain, since=now - timedelta(hours=24), min_gap_sec=300,
    )
    assert len(events) == 1
    last = events[-1]
    assert last["recovered"] is False
    assert last["ended_at"] is None
    assert last["duration_sec"] > 6000   # ~2h

    await db["sidecar_checkin_audit"].delete_many({"runtime": brain})


@pytest.mark.asyncio
async def test_dense_checkins_produce_no_outages():
    """Check-ins every 60s for the last 10 minutes should produce
    zero outage events when min_gap_sec is 300."""
    from db import db
    from routes.brain_outages import _outages_for_brain

    brain = "alpha"
    await db["sidecar_checkin_audit"].delete_many({"runtime": brain})

    now = datetime.now(timezone.utc)
    rows = []
    for i in range(10):
        ts = now - timedelta(seconds=60 * i)
        rows.append({"runtime": brain, "ts": ts.isoformat()})
    await db["sidecar_checkin_audit"].insert_many(rows)

    events = await _outages_for_brain(
        brain, since=now - timedelta(hours=1), min_gap_sec=300,
    )
    assert events == [], (
        f"dense check-ins should produce no outage events; got {events}"
    )

    await db["sidecar_checkin_audit"].delete_many({"runtime": brain})


def test_endpoint_returns_expected_shape(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/brain-outages?hours=24", timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    for k in (
        "window_hours", "min_gap_sec", "per_brain",
        "fleet_summary", "doctrine", "computed_at",
    ):
        assert k in body, f"missing {k!r} in response"
    assert body["doctrine"] == "advisory_observability_only"
    assert "brains_currently_down" in body["fleet_summary"]
    assert "total_outages" in body["fleet_summary"]
    # Every known brain present in per_brain.
    from namespaces import DISCUSSION_PARTICIPANTS
    for brain in DISCUSSION_PARTICIPANTS:
        assert brain in body["per_brain"]
        pb = body["per_brain"][brain]
        for k in (
            "outage_count", "total_outage_sec", "longest_outage_sec",
            "currently_down", "events",
        ):
            assert k in pb
