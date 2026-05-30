"""Sidecar-checkin audit + imposter-scan tripwires (2026-05-30).

Background:
    Alpha's preview pod was POSTing to prod MC alongside the real
    prod pod for an entire debugging session, both authenticating
    with the same `ALPHA_MC_INGEST_TOKEN`. Alpha's team fixed it
    on their side (preview pods now skip the checkin loop), but
    we also added MC-side defense in depth:

      1. Audit log: every checkin appends to `sidecar_checkin_audit`
         with source_ip + process_identity + stamp env/git/pip
         fingerprints. The upserted `sidecar_checkins` doc still
         exists but it overwrites — the audit collection keeps the
         full trail.
      2. Imposter scan: aggregates the audit by identity buckets
         and flags `imposter_suspected=true` when more than one
         distinct identity sustains ≥3 checkins in the window.

These tripwires lock both behaviors so they cannot silently
regress.
"""
from __future__ import annotations

import inspect

import pytest

from db import db
from shared.runtime import sidecar_checkin as mod


pytestmark = [pytest.mark.tripwire]


# ──────────────────────── SOURCE-SCAN INVARIANTS ────────────────────────


def test_checkin_handler_records_source_ip():
    """The POST handler MUST capture source IP at the edge (X-Forwarded-For
    fallback to client.host) and persist it to the audit collection."""
    src = inspect.getsource(mod.post_sidecar_checkin)
    assert "x-forwarded-for" in src.lower(), (
        "DOCTRINE VIOLATION: source-IP capture removed from the "
        "checkin handler. Defense-in-depth against rogue sidecars "
        "impersonating a brain is gone."
    )
    assert "sidecar_checkin_audit" in src, (
        "DOCTRINE VIOLATION: audit-log insert removed from checkin "
        "handler. The upserted doc loses dupe-pod evidence on every "
        "write — without the audit append we can't catch the Alpha "
        "preview-impersonation pattern."
    )


def test_audit_insert_is_best_effort():
    """Audit-log failure MUST NEVER block a legitimate checkin.
    Source-scan: the insert is inside a try/except that doesn't
    re-raise."""
    src = inspect.getsource(mod.post_sidecar_checkin)
    # Find the audit insert. Ensure it sits inside a try block whose
    # except does NOT re-raise.
    assert 'await db["sidecar_checkin_audit"].insert_one' in src
    # Find the except clause that follows the audit insert.
    audit_idx = src.index('await db["sidecar_checkin_audit"].insert_one')
    tail = src[audit_idx:audit_idx + 800]
    assert "except Exception" in tail, (
        "audit insert lacks an except clause"
    )
    # The except body must be a `pass` (or comment), not `raise`.
    except_idx = tail.index("except Exception")
    except_body = tail[except_idx:except_idx + 200]
    assert "raise" not in except_body, (
        "DOCTRINE VIOLATION: audit insert exception re-raises. "
        "An audit-log failure must never block a checkin."
    )


def test_imposter_scan_threshold_is_sane():
    """Imposter scan MUST require sustained dupes (>=3 checkins per
    bucket) before flagging — otherwise one rogue probe pings would
    cry wolf and the operator dashboard becomes noise."""
    src = inspect.getsource(mod.get_sidecar_checkin_imposter_scan)
    assert 'b["count"] >= 3' in src, (
        "imposter-scan threshold has been weakened below 3. False "
        "positives will swamp the operator dashboard."
    )
    assert "len(sustained) > 1" in src, (
        "imposter_suspected logic has changed — it must require "
        "MORE THAN ONE sustained identity, not just any sustained one"
    )


# ──────────────────────── BEHAVIORAL INVARIANTS ────────────────────────


@pytest.mark.asyncio
async def test_audit_collection_writes_on_checkin(monkeypatch):
    """A successful POST to the checkin handler MUST write one row
    to `sidecar_checkin_audit` carrying source_ip + stamp fingerprints."""
    import uuid
    from unittest.mock import MagicMock

    monkeypatch.setenv("ALPHA_INGEST_TOKEN", "tripwire-token")

    marker = f"tripwire-{uuid.uuid4()}"
    body = mod.CheckinRequest(stamp={
        "app_name": "alpha",
        "env_name": "prod",
        "git_sha": marker,
        "platform": "test",
        "mc_url": "https://mission.risedual.ai",
        "db_name": "risedual_db",
        "broker_mode": "paper",
        "sidecar_room": "test-room",
        "sidecar_version": "tripwire",
        "policy_hash": mod.policy_hash(),
        "local_execution_authority": False,
        "timestamp_ms": 1700000000000,
        "process_identity": {
            "pid": 99999,
            "hostname": "tripwire-host",
            "process_boot_at": "2026-05-30T00:00:00+00:00",
        },
    })
    # Fake Request — only the bits the handler actually reads.
    fake_req = MagicMock()
    fake_req.headers = {"x-forwarded-for": "203.0.113.42"}
    fake_req.client = MagicMock(host="10.0.0.1")

    resp = await mod.post_sidecar_checkin(
        request=fake_req, body=body, brain="alpha",
        x_runtime_token="tripwire-token",
    )
    assert resp.verdict == "prod"

    audit_row = await db["sidecar_checkin_audit"].find_one(
        {"stamp_git_sha": marker}, {"_id": 0},
    )
    assert audit_row is not None, (
        "audit row not written — defense-in-depth audit is broken"
    )
    assert audit_row["runtime"] == "alpha"
    assert audit_row["verdict"] == "prod"
    # X-Forwarded-For should win over client.host
    assert audit_row["source_ip"] == "203.0.113.42"
    pi = audit_row.get("process_identity") or {}
    assert pi.get("pid") == 99999
    assert pi.get("hostname") == "tripwire-host"

    await db["sidecar_checkin_audit"].delete_many({"stamp_git_sha": marker})


@pytest.mark.asyncio
async def test_imposter_scan_flags_two_sustained_identities():
    """Plant two distinct (pid, hostname) tuples each with 3+
    checkins → scan must flag imposter_suspected=True."""
    import uuid
    from datetime import datetime, timezone

    runtime = "alpha"
    marker = f"imposter-tripwire-{uuid.uuid4()}"
    now_epoch = datetime.now(timezone.utc).timestamp()
    docs = []
    for pid, host in ((1111, "good-prod-pod"), (2222, "rogue-preview-pod")):
        for i in range(4):
            docs.append({
                "runtime": runtime,
                "ts": datetime.now(timezone.utc).isoformat(),
                "ts_epoch": now_epoch - (i * 60),
                "source_ip": f"10.0.{pid}.{i}",
                "verdict": "prod",
                "errors": [],
                "stamp_env_name": "prod",
                "stamp_git_sha": marker,
                "stamp_pip_sha": f"sha-{pid}",
                "process_identity": {
                    "pid": pid, "hostname": host,
                    "process_boot_at": "2026-05-30T00:00:00+00:00",
                },
            })
    await db["sidecar_checkin_audit"].insert_many(docs)
    try:
        data = await mod.get_sidecar_checkin_imposter_scan(
            brain=runtime, hours=1, _user={"sub": "tripwire-operator"},
        )
        sustained_pids = [
            b["process_identity"].get("pid")
            for b in data["buckets"]
            if b["count"] >= 3
            and b.get("process_identity", {}).get("pid") in (1111, 2222)
        ]
        assert 1111 in sustained_pids, "good pod not bucketed"
        assert 2222 in sustained_pids, "rogue pod not bucketed"
        assert data["imposter_suspected"] is True, (
            "imposter scan failed to flag two sustained identities"
        )
    finally:
        await db["sidecar_checkin_audit"].delete_many({"stamp_git_sha": marker})
