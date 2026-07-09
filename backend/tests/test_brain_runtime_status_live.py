"""Live integration tests for GET /api/admin/runtime/{brain}/status.

Guards the two-part P0 hotfix in `routes/brain_runtime.py`:

  Part 1 (already deployed): removed unbounded
    `count_documents({"stack_canonical": X})` and added the
    composite (stack_canonical, ingest_ts) index. `total_intents`
    is permanently None.

  Part 2 (this iteration): bounded the `latest_intent` find_one to
    a 48h `ingest_ts` window so it uses `ingest_ts_idx` and never
    full-scans, even on Atlas with a hot composite build.

Coverage:
  - Auth-gated (401 without token, works with admin JWT)
  - All 4 in-process brains (camino/barracuda/hellcat/gto)
    respond ok:true
  - Each response < 2s (the whole point of the fix)
  - Payload shape: intents.latest_ts / latest_age_s / last_1h /
    last_24h present, intents.total is None
  - Silent-brain edge case: on a fresh stack_canonical with no
    rows in the last 48h, the code path returns latest_ts=None
    gracefully (unit-level test against `_build_in_process_status`
    guarded by a stack_canonical monkeypatch).
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict

import pytest
import requests


BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Frontend .env holds the public URL; fall back for local dev
    # if the env didn't propagate. Do NOT hardcode a default.
    with open("/app/frontend/.env", "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                break

ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PASSWORD = "risedual-admin-2026"

BRAINS = ("camino", "barracuda", "hellcat", "gto")
LATENCY_BUDGET_S = 2.0


# ─────────────────── fixtures ───────────────────

@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def admin_token(api) -> str:
    r = api.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=10,
    )
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code} {r.text[:200]}")
    tok = r.json().get("access_token") or r.json().get("token")
    assert tok, f"no access_token in login response: {r.json()}"
    return tok


@pytest.fixture(scope="module")
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


# ─────────────────── Auth gating ───────────────────

def test_status_requires_auth_no_token(api):
    """Endpoint must be 401/403 without any Authorization header."""
    r = api.get(f"{BASE_URL}/api/admin/runtime/camino/status", timeout=10)
    assert r.status_code in (401, 403), (
        f"expected 401/403 unauth, got {r.status_code}: {r.text[:200]}"
    )


def test_status_rejects_bad_token(api):
    r = api.get(
        f"{BASE_URL}/api/admin/runtime/camino/status",
        headers={"Authorization": "Bearer not-a-real-jwt"},
        timeout=10,
    )
    assert r.status_code in (401, 403)


# ─────────────────── All 4 brains fast + ok ───────────────────

@pytest.mark.parametrize("brain", BRAINS)
def test_status_ok_true_and_fast(api, auth_headers, brain):
    """The core P0 assertion — every brain returns ok:true in <2s."""
    t0 = time.perf_counter()
    r = api.get(
        f"{BASE_URL}/api/admin/runtime/{brain}/status",
        headers=auth_headers,
        timeout=10,
    )
    dt = time.perf_counter() - t0

    assert r.status_code == 200, (
        f"{brain}: status={r.status_code} body={r.text[:300]}"
    )
    body = r.json()
    assert body.get("brain") == brain
    assert body.get("ok") is True, (
        f"{brain} not ok: error={body.get('error')} "
        f"error_detail={body.get('error_detail')}"
    )
    assert body.get("doctrine") == "in_process_runtime_status"
    assert body.get("_proxied_from") == "in_process"
    assert dt < LATENCY_BUDGET_S, (
        f"{brain} latency {dt:.2f}s exceeds {LATENCY_BUDGET_S}s budget"
    )


@pytest.mark.parametrize("brain", BRAINS)
def test_status_payload_shape(api, auth_headers, brain):
    """Payload sections + intents keys the tile needs."""
    r = api.get(
        f"{BASE_URL}/api/admin/runtime/{brain}/status",
        headers=auth_headers,
        timeout=10,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    payload: Dict[str, Any] = body["payload"]

    # Required top-level sections
    for section in ("identity", "seats", "heartbeat", "intents", "in_process_runner"):
        assert section in payload, f"missing section {section!r} for {brain}"

    intents = payload["intents"]
    # Part-1 doctrine: total is None permanently
    assert intents["total"] is None, (
        f"{brain}: intents.total must be None (was {intents['total']!r}) — "
        "the unbounded lifetime count was removed as a P1 timeout fix."
    )
    # Window counts always present as ints
    for k in ("last_1h", "last_24h"):
        assert isinstance(intents[k], int), (
            f"{brain}: intents.{k} must be int, got {type(intents[k]).__name__}"
        )
        assert intents[k] >= 0

    # latest_ts / latest_age_s: keys MUST be present. Values may be
    # None if brain has been silent >48h; otherwise both are populated.
    assert "latest_ts" in intents
    assert "latest_age_s" in intents
    if intents["latest_ts"] is not None:
        assert isinstance(intents["latest_ts"], str)
        assert isinstance(intents["latest_age_s"], (int, float))
        # And it MUST be within the 48h window per the P0 fix bound.
        assert intents["latest_age_s"] <= 48 * 3600 + 60, (
            f"{brain}: latest_age_s {intents['latest_age_s']}s exceeds 48h — "
            "the find_one cutoff_48h bound is not being applied."
        )
    else:
        # Silent brain — age must also be None (never bogus)
        assert intents["latest_age_s"] is None


def test_status_unknown_brain_is_404(api, auth_headers):
    r = api.get(
        f"{BASE_URL}/api/admin/runtime/not-a-real-brain/status",
        headers=auth_headers,
        timeout=10,
    )
    assert r.status_code == 404


# ─────────────────── Silent-brain edge case (unit level) ───────────────────

@pytest.mark.asyncio
async def test_silent_brain_returns_latest_ts_none(monkeypatch):
    """Direct call into `_build_in_process_status` with a
    stack_canonical that maps to zero recent rows — the fixed
    find_one must return None (not raise) and the assembled
    payload must carry `latest_ts=None`, `latest_age_s=None`.

    This is the exact edge case the P0 fix is designed to handle
    without doing a collection full-scan: a brain that hasn't
    emitted in >48h.
    """
    # Monkeypatch the canonicalizer so we route the brain lookup at
    # an unused canonical partition — no rows, guaranteed silent.
    from routes import brain_runtime as br
    import shared.brain_legend as bl

    unused = f"test-silent-brain-{int(time.time())}"

    monkeypatch.setattr(bl, "canonicalize_stack", lambda _b: unused)

    # Call the function directly — bypasses the router auth, hits
    # real DB with a canonical value that has 0 rows.
    payload = await br._build_in_process_status("camino")

    intents = payload["intents"]
    assert intents["latest_ts"] is None, (
        f"silent-brain: expected latest_ts=None got {intents['latest_ts']!r}"
    )
    assert intents["latest_age_s"] is None
    assert intents["total"] is None
    assert intents["last_1h"] == 0
    assert intents["last_24h"] == 0
    assert intents["by_action"] == {}


# ─────────────────── Composite index sanity ───────────────────

@pytest.mark.asyncio
async def test_composite_index_exists_on_shared_intents():
    """Part-1 fix invariant: `(stack_canonical, ingest_ts -1)`
    index must exist. The find_one bound in Part-2 also depends
    on the plain `(ingest_ts -1)` index existing."""
    from db import db
    idx_info = await db.shared_intents.index_information()
    names = set(idx_info.keys())
    assert "shared_intents_stack_canonical_ingest_ts_idx" in names, (
        f"composite index missing; have: {sorted(names)}"
    )
    assert "shared_intents_ingest_ts_idx" in names, (
        f"ingest_ts index missing; have: {sorted(names)}"
    )
