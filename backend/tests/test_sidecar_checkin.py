"""HTTP tests for `/api/admin/runtime/sidecar-checkin` — the Portable
Survival Layer companion that lets MC answer "who's PROD vs preview"
at a glance.
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict

import requests

from shared.runtime.platform_survival import RuntimeStamp, policy_hash


# ───── helpers ────────────────────────────────────────────────────────


def _prod_stamp() -> dict:
    return {
        "app_name": "alpha",
        "env_name": "prod",
        "git_sha": "abc12345",
        "platform": "railway",
        "mc_url": "https://mission.risedual.ai",
        "db_name": "risedual_prod",
        "broker_mode": "paper",
        "sidecar_room": "alpha-room",
        "sidecar_version": "1.0.0",
        "policy_hash": policy_hash(),
        "local_execution_authority": False,
        "timestamp_ms": int(time.time() * 1000),
    }


def _preview_stamp() -> dict:
    s = _prod_stamp()
    s["env_name"] = "preview"
    s["mc_url"] = "https://preview.mission.risedual.ai"
    s["db_name"] = "preview"
    return s


def _camino_token() -> str:
    tok = os.environ.get("CAMINO_INGEST_TOKEN") or ""
    if not tok:
        # Read from .env directly so the test works in CI where this
        # env var may not be pre-exported.
        try:
            with open("/app/backend/.env") as f:
                for line in f:
                    if line.startswith("CAMINO_INGEST_TOKEN="):
                        tok = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
        except OSError:
            pass
    return tok


# ───── POST contract ──────────────────────────────────────────────────


def test_post_rejects_bad_token(base_url):
    r = requests.post(
        f"{base_url}/api/admin/runtime/sidecar-checkin/alpha",
        json={"stamp": _prod_stamp()},
        headers={"X-Runtime-Token": "definitely-not-the-real-token"},
        timeout=15,
    )
    assert r.status_code == 401, r.text


def test_post_rejects_unknown_brain(base_url):
    r = requests.post(
        f"{base_url}/api/admin/runtime/sidecar-checkin/notabrain",
        json={"stamp": _prod_stamp()},
        headers={"X-Runtime-Token": _camino_token() or "x"},
        timeout=15,
    )
    assert r.status_code == 404


def test_post_prod_stamp_records_prod_verdict(base_url):
    tok = _camino_token()
    if not tok:
        return  # env not configured in this runner

    r = requests.post(
        f"{base_url}/api/admin/runtime/sidecar-checkin/alpha",
        json={"stamp": _prod_stamp()},
        headers={"X-Runtime-Token": tok},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "prod"
    assert body["policy_hash_match"] is True
    assert body["errors"] == []
    assert body["ok"] is True
    assert body["mc_policy_hash"] == policy_hash()


def test_post_preview_stamp_records_preview_verdict(base_url):
    tok = _camino_token()
    if not tok:
        return

    r = requests.post(
        f"{base_url}/api/admin/runtime/sidecar-checkin/alpha",
        json={"stamp": _preview_stamp()},
        headers={"X-Runtime-Token": tok},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "preview"
    assert "ENV_NOT_PROD" in body["errors"]


def test_post_policy_drift_when_hash_mismatch(base_url):
    tok = _camino_token()
    if not tok:
        return

    drifted = _prod_stamp()
    drifted["policy_hash"] = "stale_hash_deadbeef" * 2  # arbitrary wrong hash
    r = requests.post(
        f"{base_url}/api/admin/runtime/sidecar-checkin/alpha",
        json={"stamp": drifted},
        headers={"X-Runtime-Token": tok},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # The validator itself returns ok=True (it doesn't check policy_hash
    # against MC's — that's MC's job here). We classify as policy_drift.
    assert body["verdict"] == "policy_drift"
    assert body["policy_hash_match"] is False


def test_post_invalid_when_stamp_shape_wrong(base_url):
    tok = _camino_token()
    if not tok:
        return

    r = requests.post(
        f"{base_url}/api/admin/runtime/sidecar-checkin/alpha",
        json={"stamp": {"app_name": "alpha"}},  # missing all required fields
        headers={"X-Runtime-Token": tok},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "invalid"
    assert body["ok"] is False
    assert any(e.startswith("STAMP_SHAPE_INVALID") for e in body["errors"])


# ───── GET contract ───────────────────────────────────────────────────


def test_get_list_requires_auth(base_url):
    r = requests.get(f"{base_url}/api/admin/runtime/sidecar-checkin", timeout=15)
    assert r.status_code in (401, 403)


def test_get_list_returns_one_row_per_brain(auth_client, base_url):
    r = auth_client.get(f"{base_url}/api/admin/runtime/sidecar-checkin", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert "mc_policy_hash" in body
    assert "rows" in body
    runtimes = {row["runtime"] for row in body["rows"]}
    # Every known brain has a row (never-seen brains get verdict="never")
    assert {"alpha", "camaro", "chevelle", "redeye"}.issubset(runtimes)


def test_get_single_brain_returns_never_for_silent_sidecar(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/runtime/sidecar-checkin/redeye",
        timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["runtime"] == "redeye"
    # redeye hasn't checked in in this test run (and we don't clean up
    # alpha to keep tests independent) — verdict is either "never" or
    # whatever was previously persisted. Just assert the contract.
    assert body["verdict"] in ("never", "prod", "preview", "policy_drift", "invalid")
    assert "mc_policy_hash" in body


def test_get_single_brain_rejects_unknown(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/runtime/sidecar-checkin/notabrain",
        timeout=15,
    )
    assert r.status_code == 404


# ───── Round-trip: POST then GET reflects the new row ─────────────────


def test_post_then_get_reflects_latest_stamp(auth_client, base_url):
    tok = _camino_token()
    if not tok:
        return

    # Record a clean prod check-in
    r = requests.post(
        f"{base_url}/api/admin/runtime/sidecar-checkin/alpha",
        json={"stamp": _prod_stamp()},
        headers={"X-Runtime-Token": tok},
        timeout=15,
    )
    assert r.status_code == 200

    # GET single-brain detail
    r = auth_client.get(
        f"{base_url}/api/admin/runtime/sidecar-checkin/alpha",
        timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["runtime"] == "alpha"
    assert body["verdict"] == "prod"
    assert body["freshness"] in ("fresh", "stale")  # just persisted → fresh
    assert body["checkin_count"] >= 1
    assert body["stamp"]["env_name"] == "prod"
    assert body["stamp"]["mc_url"].startswith("https://mission.risedual.ai")



# ───── Heartbeat side-effect — 2026-02-19 ─────────────────────────────
# A successful sidecar check-in is unambiguous proof of life. The
# handler MUST also bump `shared_heartbeats.last_seen` so the LivePulse
# LIVE/STALE/DEAD badge stays in sync with the Sidecar Imposter Scan.
# Before this side-effect, brains whose sidecars hit ONLY the
# check-in endpoint (and not /heartbeat-ping) appeared DEAD on the
# runtime table despite their pod being healthy — the REDEYE silence
# pattern observed on 2026-06-02.


def test_post_sidecar_checkin_also_bumps_heartbeat(auth_client, base_url):
    """A successful sidecar check-in must refresh
    /api/heartbeat-status/{brain} immediately. Read via the public
    heartbeat-status endpoint (no auth) so the test pins the
    operator-visible behavior, not the storage detail.
    """
    tok = _camino_token()
    if not tok:
        return

    # Fire a clean prod check-in.
    r = requests.post(
        f"{base_url}/api/admin/runtime/sidecar-checkin/alpha",
        json={"stamp": _prod_stamp()},
        headers={"X-Runtime-Token": tok},
        timeout=15,
    )
    assert r.status_code == 200, r.text

    # Read heartbeat-status. Heartbeat age must be < a few seconds —
    # i.e., the check-in just bumped it.
    r = requests.get(
        f"{base_url}/api/heartbeat-status/alpha",
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    hb_age = body.get("heartbeat_age_seconds")
    assert hb_age is not None, body
    assert hb_age < 30, (
        f"sidecar-checkin did not refresh heartbeat row "
        f"(heartbeat_age_seconds={hb_age!r}); the LIVE/STALE/DEAD "
        f"badge will stay stuck on a check-in-only brain"
    )
