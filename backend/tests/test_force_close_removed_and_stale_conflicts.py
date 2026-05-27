"""Tripwires for 2026 doctrine cleanup:
  1. `broker_force_close_routes.py` MUST stay deleted — operator overrides
     bypass the 12-gate evaluation chain. All closes must route through
     MC's CLOSE intent verb.
  2. `GET /api/admin/conflicts/stale` is the operator-alert endpoint
     surfacing open conflicts older than the threshold so they don't
     clog the doctrine chain. Schema is locked.
"""
from __future__ import annotations

import importlib
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests

BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                break

ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL", "admin@risedual.io")
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD", "risedual-admin-2026")


def _login() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=30,
    )
    if r.status_code != 200:
        pytest.skip(f"login failed {r.status_code}: {r.text}")
    return r.json()["access_token"]


# ───────────────────────── force-close removal ─────────────────────────

@pytest.mark.tripwire
def test_broker_force_close_module_is_deleted():
    """The route file is doctrinally banned. Deleting it is the
    enforcement; re-creating it would re-open the bypass."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("routes.broker_force_close_routes")


@pytest.mark.tripwire
def test_force_close_endpoint_returns_404():
    """The `/admin/broker/force-close-all` endpoint must not be mounted.
    Closes must flow through MC's CLOSE intent verb only."""
    token = _login()
    r = requests.post(
        f"{BASE_URL}/api/admin/broker/force-close-all",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "tripwire probe", "confirm": False},
        timeout=15,
    )
    assert r.status_code == 404, (
        f"force-close endpoint must be 404; got {r.status_code} {r.text[:200]}"
    )


@pytest.mark.tripwire
def test_force_close_log_endpoint_returns_404():
    """The companion read endpoint must also be gone."""
    token = _login()
    r = requests.get(
        f"{BASE_URL}/api/admin/broker/force-close-log",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    assert r.status_code == 404, (
        f"force-close-log endpoint must be 404; got {r.status_code}"
    )


# ───────────────────────── stale conflicts endpoint ─────────────────────────

@pytest.mark.tripwire
def test_stale_conflicts_endpoint_schema():
    """Endpoint exists, requires auth, returns the locked shape."""
    # Unauthed call must be rejected.
    ru = requests.get(f"{BASE_URL}/api/admin/conflicts/stale", timeout=15)
    assert ru.status_code in (401, 403), f"must require auth; got {ru.status_code}"

    token = _login()
    r = requests.get(
        f"{BASE_URL}/api/admin/conflicts/stale?older_than_hours=24",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Locked top-level keys.
    for key in (
        "older_than_hours", "count", "oldest_age_hours",
        "by_runtime", "items", "doctrine", "generated_at",
    ):
        assert key in body, f"missing {key} in response"
    assert body["older_than_hours"] == 24.0
    assert isinstance(body["items"], list)
    assert isinstance(body["by_runtime"], dict)
    assert isinstance(body["count"], int)


@pytest.mark.tripwire
def test_stale_conflicts_only_includes_open_past_threshold():
    """Seed: one OPEN conflict 48h old (should appear), one OPEN 1h old
    (should NOT), one RESOLVED 48h old (should NOT). The endpoint must
    only return open + past-threshold rows."""
    from pymongo import MongoClient
    mongo_url = os.environ["MONGO_URL"]
    db_name = os.environ["DB_NAME"]
    client = MongoClient(mongo_url)
    coll = client[db_name]["shared_brain_conflicts"]

    tag = f"tw-stale-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    old_iso = (now - timedelta(hours=48)).isoformat()
    fresh_iso = (now - timedelta(hours=1)).isoformat()

    docs = [
        {
            "conflict_id": f"{tag}-old-open",
            "topic": tag,
            "detected_at": old_iso,
            "pair_ids": [f"{tag}-a", f"{tag}-b"],
            "participants": [
                {"opinion_id": f"{tag}-a", "runtime": "alpha", "stance": "long",
                 "confidence": 0.7, "posted_at": old_iso},
                {"opinion_id": f"{tag}-b", "runtime": "redeye", "stance": "short",
                 "confidence": 0.7, "posted_at": old_iso},
            ],
            "status": "open", "winner": None, "winning_opinion_id": None,
            "resolved_at": None, "resolved_by": None,
            "resolution_source": None, "notes": "",
        },
        {
            "conflict_id": f"{tag}-fresh-open",
            "topic": tag,
            "detected_at": fresh_iso,
            "pair_ids": [f"{tag}-c", f"{tag}-d"],
            "participants": [
                {"opinion_id": f"{tag}-c", "runtime": "alpha", "stance": "long",
                 "confidence": 0.7, "posted_at": fresh_iso},
                {"opinion_id": f"{tag}-d", "runtime": "camaro", "stance": "short",
                 "confidence": 0.7, "posted_at": fresh_iso},
            ],
            "status": "open", "winner": None, "winning_opinion_id": None,
            "resolved_at": None, "resolved_by": None,
            "resolution_source": None, "notes": "",
        },
        {
            "conflict_id": f"{tag}-old-resolved",
            "topic": tag,
            "detected_at": old_iso,
            "pair_ids": [f"{tag}-e", f"{tag}-f"],
            "participants": [
                {"opinion_id": f"{tag}-e", "runtime": "alpha", "stance": "long",
                 "confidence": 0.7, "posted_at": old_iso},
                {"opinion_id": f"{tag}-f", "runtime": "chevelle", "stance": "short",
                 "confidence": 0.7, "posted_at": old_iso},
            ],
            "status": "resolved", "winner": "alpha",
            "winning_opinion_id": f"{tag}-e",
            "resolved_at": now.isoformat(), "resolved_by": "tripwire",
            "resolution_source": "manual", "notes": "",
        },
    ]
    coll.insert_many(docs)
    try:
        token = _login()
        r = requests.get(
            f"{BASE_URL}/api/admin/conflicts/stale?older_than_hours=24&limit=1000",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Filter to just this tripwire's docs so we don't depend on
        # the DB's pre-existing conflict population (which can exceed
        # the default 200-row limit).
        ours = [c for c in body["items"] if c.get("topic") == tag]
        returned_ids = {c["conflict_id"] for c in ours}
        assert f"{tag}-old-open" in returned_ids
        assert f"{tag}-fresh-open" not in returned_ids
        assert f"{tag}-old-resolved" not in returned_ids
        # by_runtime should at least include alpha + redeye (the old-open pair).
        by_rt = body["by_runtime"]
        # Note: by_runtime aggregates ALL rows returned, not just ours,
        # so we just sanity-check the totals are sensible (>= our seeded
        # contribution). The strict ID filtering above is the real check.
        assert isinstance(by_rt, dict)
        assert by_rt.get("alpha", 0) >= 1
        assert by_rt.get("redeye", 0) >= 1
    finally:
        coll.delete_many({"topic": tag})
