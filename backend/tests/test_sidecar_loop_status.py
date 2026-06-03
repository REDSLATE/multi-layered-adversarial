"""Sidecar check-in `loop_status` extension — contract pin (2026-02-20).

Doctrine:
  The REDEYE 3-day silence pattern (2026-05-30 → 2026-06-02) revealed
  a real gap: MC could see a brain's sidecar pinging cleanly but
  had no fresh view of the brain's INTERNAL loops (decision,
  opinion, intent, sovereign-tick). The "LAST RECEIPT" column reads
  sovereign contributions, which silenced 3 days ago; the STATUS
  column reads heartbeats, which the side-effect bumps every
  check-in. So MC dashboard simultaneously showed REDEYE LIVE 11s
  AND last receipt 3d ago — two contradictory truths.

  This extension lets brains attest to their own internal loop
  freshness on every check-in. MC notarizes it and surfaces a
  derived `loop_health` band ("green" / "amber" / "red" / "unknown")
  on the operator dashboard.

What this pins:
  1. The MC schema accepts an optional `loop_status` block alongside
     `stamp`. Backward-compat (no `loop_status` is fine).
  2. The persisted `sidecar_checkins.{runtime}` doc carries both
     `loop_status` (raw) and `loop_health` (derived band).
  3. The emission-diagnose endpoint surfaces both fields.
  4. The `loop_health` band correctly maps known cases:
       - missing loop_status        → "unknown"
       - tick_loop_healthy=false    → "red"
       - no sovereign timestamp     → "red"
       - sovereign < 1h ago         → "green"
       - sovereign 1h-6h ago        → "amber"
       - sovereign > 6h ago         → "red"
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# ─── unit: the band derivation ────────────────────────────────────────


@pytest.mark.tripwire
def test_loop_health_band_derivation():
    """The `_loop_health_from` helper must map every documented case
    to its band — protects the contract MC promised the brain teams."""
    from shared.runtime.sidecar_checkin import _loop_health_from
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)

    # 1. No loop_status at all → unknown
    assert _loop_health_from(None, now) == "unknown"
    # Empty dict is also unknown — brain shipped the block but told
    # us nothing. Silence is not implicit failure.
    assert _loop_health_from({}, now) == "unknown"
    # Brain shipped block but no sovereign timestamp yet → unknown
    # (brain may not have ticked since boot).
    assert _loop_health_from(
        {"tick_loop_healthy": True}, now,
    ) == "unknown"

    # 2. Brain reports unhealthy → red (overrides any timestamp)
    fresh_iso = (now - timedelta(seconds=10)).isoformat()
    assert _loop_health_from(
        {"tick_loop_healthy": False, "last_sovereign_contribution_at": fresh_iso},
        now,
    ) == "red"

    # 3. Sovereign < 1h ago, healthy True → green
    iso_30m = (now - timedelta(minutes=30)).isoformat()
    assert _loop_health_from(
        {"tick_loop_healthy": True, "last_sovereign_contribution_at": iso_30m},
        now,
    ) == "green"

    # 4. Sovereign 1h-6h ago → amber
    iso_3h = (now - timedelta(hours=3)).isoformat()
    assert _loop_health_from(
        {"tick_loop_healthy": True, "last_sovereign_contribution_at": iso_3h},
        now,
    ) == "amber"

    # 5. Sovereign > 6h ago → red (the REDEYE 3-day case)
    iso_3d = (now - timedelta(days=3)).isoformat()
    assert _loop_health_from(
        {"tick_loop_healthy": True, "last_sovereign_contribution_at": iso_3d},
        now,
    ) == "red"

    # 6. Malformed timestamp → red (graceful, not crash)
    assert _loop_health_from(
        {"tick_loop_healthy": True, "last_sovereign_contribution_at": "not-a-date"},
        now,
    ) == "red"


# ─── source tripwire: schema field + persistence ──────────────────────


@pytest.mark.tripwire
def test_loop_status_wired_into_checkin_schema():
    """The `LoopStatus` class must exist, be referenced from
    `CheckinRequest`, and be persisted on the upsert."""
    with open("/app/backend/shared/runtime/sidecar_checkin.py") as f:
        src = f.read()
    assert "class LoopStatus(BaseModel):" in src, (
        "LoopStatus schema is missing — brain teams have no contract "
        "to ship against"
    )
    assert "loop_status: Optional[LoopStatus]" in src, (
        "CheckinRequest no longer accepts the optional loop_status field"
    )
    # Persisted on the upsert
    assert '"loop_status": loop_status_dict' in src, (
        "loop_status block is not being written into the "
        "sidecar_checkins doc — operator will never see brain-side "
        "loop liveness signals"
    )
    assert '"loop_health": loop_health' in src, (
        "loop_health derived band is not being persisted"
    )


@pytest.mark.tripwire
def test_emission_diagnose_surfaces_loop_status():
    """The emission-diagnose endpoint must surface `loop_status` and
    `loop_health` so the Diagnostics dashboard can read them."""
    with open("/app/backend/routes/brain_emission_diagnose.py") as f:
        src = f.read()
    assert '"loop_status"' in src, (
        "emission-diagnose doesn't return loop_status — Diagnostics "
        "dashboard has nowhere to read fresh brain-side signals"
    )
    assert '"loop_health"' in src, (
        "emission-diagnose doesn't return loop_health — operator "
        "loses the at-a-glance band"
    )


# ─── behavioral: POST + GET round-trip ────────────────────────────────


import os
import requests  # noqa: E402


def _alpha_token() -> str:
    return os.environ.get("ALPHA_INGEST_TOKEN", "")


def _prod_stamp(policy_hash_value: str) -> dict:
    """Minimal valid prod stamp shape."""
    return {
        "app_name": "alpha",
        "env_name": "prod",
        "git_sha": "loopstatus_e2e",
        "platform": "railway",
        "mc_url": "https://mission.risedual.ai",
        "db_name": "risedual_prod",
        "broker_mode": "paper",
        "sidecar_room": "alpha-room",
        "sidecar_version": "1.0.0",
        "policy_hash": policy_hash_value,
        "local_execution_authority": False,
        "timestamp_ms": 1733176800000,
    }


def test_post_loop_status_roundtrips_to_diagnose(auth_client, base_url):
    """A POST with `loop_status` must persist and re-surface on
    /api/admin/brain/emission-diagnose/{brain}."""
    tok = _alpha_token()
    if not tok:
        pytest.skip("ALPHA_INGEST_TOKEN not set in this env")
    from shared.runtime.platform_survival import policy_hash
    fresh_iso = datetime.now(timezone.utc).isoformat()

    body = {
        "stamp": _prod_stamp(policy_hash()),
        "loop_status": {
            "last_decision_log_at": fresh_iso,
            "last_opinion_at": fresh_iso,
            "last_intent_at": fresh_iso,
            "last_sovereign_contribution_at": fresh_iso,
            "tick_loop_healthy": True,
            "tick_loop_last_error": None,
        },
    }
    r = requests.post(
        f"{base_url}/api/admin/runtime/sidecar-checkin/alpha",
        json=body,
        headers={"X-Runtime-Token": tok},
        timeout=15,
    )
    assert r.status_code == 200, r.text

    # Read via emission-diagnose
    r2 = auth_client.get(
        f"{base_url}/api/admin/brain/emission-diagnose/alpha", timeout=15,
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    sc = body2.get("sidecar_checkin") or {}
    assert sc.get("loop_health") == "green", (
        f"loop_health should be 'green' for fresh+healthy timestamps; "
        f"got {sc.get('loop_health')!r}"
    )
    ls = sc.get("loop_status") or {}
    assert ls.get("tick_loop_healthy") is True
    assert ls.get("last_sovereign_contribution_at") == fresh_iso


def test_post_without_loop_status_still_works(auth_client, base_url):
    """Backward-compat: a brain that doesn't ship the extension must
    keep working. `loop_health` defaults to 'unknown' for that brain."""
    tok = _alpha_token()
    if not tok:
        pytest.skip("ALPHA_INGEST_TOKEN not set in this env")
    from shared.runtime.platform_survival import policy_hash

    body = {"stamp": _prod_stamp(policy_hash())}  # NO loop_status
    r = requests.post(
        f"{base_url}/api/admin/runtime/sidecar-checkin/alpha",
        json=body,
        headers={"X-Runtime-Token": tok},
        timeout=15,
    )
    assert r.status_code == 200, r.text

    r2 = auth_client.get(
        f"{base_url}/api/admin/brain/emission-diagnose/alpha", timeout=15,
    )
    assert r2.status_code == 200, r2.text
    sc = r2.json().get("sidecar_checkin") or {}
    assert sc.get("loop_status") is None
    assert sc.get("loop_health") == "unknown", (
        "brain without loop_status block must default to "
        "'unknown' band — never 'red' or a crash"
    )
