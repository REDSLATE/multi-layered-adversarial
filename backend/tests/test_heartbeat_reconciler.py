"""Heartbeat reconciler contract pin (2026-02-20).

Doctrine:
  The sidecar check-in handler bumps `shared_heartbeats.last_seen`
  as a side-effect on every successful check-in. That side-effect
  is wrapped in try/except so a transient Mongo glitch silently
  swallows the heartbeat update — the operator sees fresh imposter
  scan check-ins for a brain while the LIVE/STALE/DEAD badge says
  STALE/DEAD. This worker closes the durability gap by deriving
  heartbeat freshness from `sidecar_checkin_audit` rows on a 60s tick.

What this pins:
  1. `perform_reconcile()` bumps shared_heartbeats from a newer audit row.
  2. It refuses to bump from rows older than `max_age_sec` (no
     "rewrite history" failure mode).
  3. It's a no-op when the heartbeat is already at least as fresh.
  4. It correctly handles brains with no audit rows (no_audit list).
  5. The admin route `POST /api/admin/heartbeat-reconcile/run` is wired.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.tripwire
def test_reconciler_helper_exists_and_is_wired():
    """The reconciler must exist and be started from server.py."""
    with open("/app/backend/shared/runtime/heartbeat_reconciler.py") as f:
        rec_src = f.read()
    assert "async def perform_reconcile(" in rec_src
    assert "def start_worker(" in rec_src
    assert '"heartbeat_reconciler"' in rec_src, (
        "detail.source label is missing — operator loses the ability "
        "to tell reconciled bumps apart from real pings"
    )

    with open("/app/backend/server.py") as f:
        server_src = f.read()
    assert "heartbeat_reconciler import" in server_src, (
        "server.py never imports the reconciler — boot wiring missing"
    )
    assert "_start_heartbeat_reconciler" in server_src, (
        "reconciler worker is not started on app boot"
    )
    assert "heartbeat_reconciler_admin_router" in server_src, (
        "admin route for on-demand reconcile is not included"
    )


def _purge_brain(db, brain: str):
    """Wipe both surfaces for one brain so the test starts clean.
    Async helper — caller must await."""
    return asyncio.gather(
        db["sidecar_checkin_audit"].delete_many({"runtime": brain}),
        db["shared_heartbeats"].delete_one({"runtime": brain}),
    )


@pytest.mark.asyncio
async def test_reconciler_bumps_when_audit_is_newer():
    """Stale heartbeat + fresh audit row → reconciler bumps the
    heartbeat to match the audit timestamp. The exact symptom the
    REDEYE 2026-06-03 screenshot was meant to fix."""
    from db import db
    from shared.runtime.heartbeat_reconciler import perform_reconcile

    brain = "alpha"
    await _purge_brain(db, brain)

    now = datetime.now(timezone.utc)
    # Audit row: 30 seconds ago (well within max_age_sec=1800).
    audit_iso = (now - timedelta(seconds=30)).isoformat()
    # Heartbeat row: 10 minutes ago (stale per the new 120s / 300s bands).
    stale_hb_iso = (now - timedelta(minutes=10)).isoformat()

    await db["sidecar_checkin_audit"].insert_one({
        "runtime": brain, "ts": audit_iso,
        "ts_epoch": (now - timedelta(seconds=30)).timestamp(),
        "verdict": "prod", "source_ip": "10.0.0.1",
    })
    await db["shared_heartbeats"].update_one(
        {"runtime": brain},
        {"$set": {"runtime": brain, "last_seen": stale_hb_iso, "status": "ok"}},
        upsert=True,
    )

    result = await perform_reconcile(max_age_sec=1800)
    bumped_brains = [b["brain"] for b in result["bumped"]]
    assert brain in bumped_brains, (
        f"reconciler did not bump {brain}; result={result}"
    )

    # Verify the heartbeat doc now reflects the audit timestamp.
    hb = await db["shared_heartbeats"].find_one({"runtime": brain}, {"_id": 0})
    assert hb["last_seen"] == audit_iso
    assert hb["detail"]["source"] == "heartbeat_reconciler"

    # Cleanup
    await _purge_brain(db, brain)


@pytest.mark.asyncio
async def test_reconciler_refuses_ancient_audit_rows():
    """The 'max_age_sec' floor prevents reconciliation from rewriting
    history. If the audit row is older than max_age_sec, leave the
    heartbeat alone — DEAD is the truth."""
    from db import db
    from shared.runtime.heartbeat_reconciler import perform_reconcile

    brain = "alpha"
    await _purge_brain(db, brain)

    now = datetime.now(timezone.utc)
    # Audit row from 2 hours ago — older than the 30-min max_age.
    ancient_iso = (now - timedelta(hours=2)).isoformat()
    await db["sidecar_checkin_audit"].insert_one({
        "runtime": brain, "ts": ancient_iso,
        "ts_epoch": (now - timedelta(hours=2)).timestamp(),
        "verdict": "prod", "source_ip": "10.0.0.1",
    })
    # No existing heartbeat row.

    result = await perform_reconcile(max_age_sec=30 * 60)  # 30 min
    assert brain not in [b["brain"] for b in result["bumped"]]
    skipped = [s["brain"] for s in result["skipped_stale"]]
    assert brain in skipped, (
        f"ancient audit row should have been classified as "
        f"skipped_stale; result={result}"
    )
    # Heartbeat doc was not created.
    hb = await db["shared_heartbeats"].find_one({"runtime": brain}, {"_id": 0})
    assert hb is None

    await _purge_brain(db, brain)


@pytest.mark.asyncio
async def test_reconciler_noop_when_heartbeat_already_fresh():
    """If shared_heartbeats.last_seen is already >= the latest audit
    timestamp, the reconciler must not write — avoids noisy updates."""
    from db import db
    from shared.runtime.heartbeat_reconciler import perform_reconcile

    brain = "alpha"
    await _purge_brain(db, brain)

    now = datetime.now(timezone.utc)
    audit_iso = (now - timedelta(seconds=120)).isoformat()
    # Heartbeat is NEWER than audit (a real ping came in after).
    fresh_hb_iso = (now - timedelta(seconds=10)).isoformat()

    await db["sidecar_checkin_audit"].insert_one({
        "runtime": brain, "ts": audit_iso,
        "ts_epoch": (now - timedelta(seconds=120)).timestamp(),
        "verdict": "prod", "source_ip": "10.0.0.1",
    })
    await db["shared_heartbeats"].update_one(
        {"runtime": brain},
        {"$set": {
            "runtime": brain, "last_seen": fresh_hb_iso, "status": "ok",
            "detail": {"source": "heartbeat_ping"},
        }},
        upsert=True,
    )

    result = await perform_reconcile(max_age_sec=1800)
    assert brain not in [b["brain"] for b in result["bumped"]]
    assert brain in result["no_change"]

    # Heartbeat doc still has the original source label.
    hb = await db["shared_heartbeats"].find_one({"runtime": brain}, {"_id": 0})
    assert hb["last_seen"] == fresh_hb_iso
    assert hb["detail"]["source"] == "heartbeat_ping", (
        "reconciler must not overwrite a fresh real ping"
    )

    await _purge_brain(db, brain)


@pytest.mark.asyncio
async def test_reconciler_lists_brains_without_any_audit():
    """A brain that has never checked in must show up in `no_audit`
    list, not as bumped or skipped."""
    from db import db
    from shared.runtime.heartbeat_reconciler import perform_reconcile
    from namespaces import DISCUSSION_PARTICIPANTS

    # Purge every brain for a clean slate.
    for b in DISCUSSION_PARTICIPANTS:
        await _purge_brain(db, b)

    result = await perform_reconcile(max_age_sec=1800)
    assert result["no_audit_count"] == len(DISCUSSION_PARTICIPANTS)
    assert set(result["no_audit"]) == set(DISCUSSION_PARTICIPANTS)
    assert result["bumped_count"] == 0


def test_admin_endpoint_status_returns_config(auth_client, base_url):
    """The /admin/heartbeat-reconcile/status endpoint must surface the
    current config so operators can see whether the worker is enabled
    + at what cadence."""
    r = auth_client.get(
        f"{base_url}/api/admin/heartbeat-reconcile/status", timeout=10,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "enabled" in body
    assert "tick_sec" in body
    assert "max_age_sec" in body
    assert body["doctrine"] == "advisory_observability_only"


def test_admin_endpoint_run_returns_summary(auth_client, base_url):
    """POST /admin/heartbeat-reconcile/run must return the same
    summary shape the worker tick logs."""
    r = auth_client.post(
        f"{base_url}/api/admin/heartbeat-reconcile/run", timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    for k in (
        "ts", "bumped_count", "bumped",
        "no_change_count", "no_change",
        "skipped_stale_count", "skipped_stale",
        "no_audit_count", "no_audit",
    ):
        assert k in body, f"missing field {k!r} in response"
