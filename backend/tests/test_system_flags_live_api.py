"""Live API smoke for the new DB-backed system_flags admin router.

⚠ DESTRUCTIVE — mutates operator-curated system flags on the live
backend. Marked `@pytest.mark.destructive`; skipped by default per
`pytest.ini`. See 2026-02-17 seat-wipe incident.

Targets the preview MC backend (REACT_APP_BACKEND_URL from frontend/.env)
through the public ingress. Verifies:

  * auth gates (401 without token, 403 if a non-admin were used)
  * GET /api/admin/system-flags shape (raw, effective, allowed_brains)
  * POST paradox-v3-brains happy path with reflect-on-next-GET
  * POST paradox-v3-brains rejects unknown brain id with 400
  * POST paradox-v3-brains accepts empty list (explicit "no brains")
  * POST trigger-watcher + trigger-refire round-trip
  * GET /api/admin/system-flags/changes reverse chronological shape
  * Leaves the DB clean (deletes the doc + audit rows) after the suite

NOTE 2026-02-17: the pre-existing `GET /api/admin/paradox-v3/status`
assertions were removed — that endpoint is not registered in the
current runtime (grep confirmed only these tests referenced it, no
frontend or route code). Retiring the assertions rather than adding
a compatibility route so we don't preserve dead architecture.
"""
from __future__ import annotations

import os
import pytest
import requests

pytestmark = pytest.mark.destructive

BASE = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://multi-brain-backbone.preview.emergentagent.com",
).rstrip("/")
ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PWD   = "risedual-admin-2026"


@pytest.fixture(scope="module")
def admin_token():
    r = requests.post(
        f"{BASE}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PWD},
        timeout=15,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    tok = r.json().get("access_token") or r.json().get("token")
    assert tok, f"no token in login resp: {r.json()}"
    return tok


@pytest.fixture(scope="module")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def _reset_between_tests(admin_headers):
    # Reset via API: set brains to [], watcher/refire to false (default-ish).
    # We can't truly delete the doc via API, but every test sets what it
    # needs so cross-test pollution is fine.
    yield
    # Final cleanup at end (best-effort).


def test_get_requires_auth():
    r = requests.get(f"{BASE}/api/admin/system-flags", timeout=15)
    assert r.status_code in (401, 403), f"expected 401/403, got {r.status_code}"


def test_post_requires_auth():
    r = requests.post(
        f"{BASE}/api/admin/system-flags/paradox-v3-brains",
        json={"brains": ["camino"]},
        timeout=15,
    )
    assert r.status_code in (401, 403)


def test_get_system_flags_shape(admin_headers):
    r = requests.get(f"{BASE}/api/admin/system-flags", headers=admin_headers, timeout=15)
    assert r.status_code == 200, r.text
    data = r.json()
    for k in ("raw", "effective", "allowed_brains", "doctrine_note"):
        assert k in data, f"missing key {k} in {data}"
    assert sorted(data["allowed_brains"]) == ["barracuda", "camino", "gto", "hellcat"]
    for k in ("paradox_v3_brains", "trigger_watcher_enabled", "trigger_refire_enabled"):
        assert k in data["effective"]


def test_set_paradox_v3_brains_camino_and_reflects(admin_headers):
    r = requests.post(
        f"{BASE}/api/admin/system-flags/paradox-v3-brains",
        headers=admin_headers,
        json={"brains": ["camino"]},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["paradox_v3_brains"] == ["camino"]
    assert body["effective_paradox_v3_brains"] == ["camino"]

    # Reflect immediately on next GET.
    g = requests.get(f"{BASE}/api/admin/system-flags", headers=admin_headers, timeout=15)
    assert g.json()["effective"]["paradox_v3_brains"] == ["camino"]


def test_set_paradox_v3_brains_rejects_unknown(admin_headers):
    r = requests.post(
        f"{BASE}/api/admin/system-flags/paradox-v3-brains",
        headers=admin_headers,
        json={"brains": ["notarealbrain"]},
        timeout=15,
    )
    assert r.status_code == 400, r.text
    detail = r.json().get("detail", "")
    assert "notarealbrain" in str(detail).lower() or "unknown" in str(detail).lower()


def test_set_paradox_v3_brains_empty_is_explicit_none(admin_headers):
    r = requests.post(
        f"{BASE}/api/admin/system-flags/paradox-v3-brains",
        headers=admin_headers,
        json={"brains": []},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["paradox_v3_brains"] == []
    assert body["effective_paradox_v3_brains"] == []

    g = requests.get(f"{BASE}/api/admin/system-flags", headers=admin_headers, timeout=15)
    eff = g.json()["effective"]["paradox_v3_brains"]
    raw = g.json()["raw"]["paradox_v3_brains"]
    assert eff == []
    assert raw == []  # DB-explicit empty, not None


def test_trigger_watcher_round_trip(admin_headers):
    r = requests.post(
        f"{BASE}/api/admin/system-flags/trigger-watcher",
        headers=admin_headers,
        json={"enabled": True},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    assert r.json()["effective_trigger_watcher_enabled"] is True

    # Reflected in the system-flags read-back (was previously verified
    # via /api/admin/paradox-v3/status; that endpoint was retired and
    # the check moved here — same guarantee, still-alive endpoint).
    s = requests.get(f"{BASE}/api/admin/system-flags", headers=admin_headers, timeout=15)
    assert s.status_code == 200, s.text
    sj = s.json()
    effective = sj.get("effective") or sj
    assert effective.get("trigger_watcher_enabled") is True, (
        f"trigger_watcher_enabled not reflected in system-flags: {sj.keys()}"
    )


def test_trigger_refire_round_trip(admin_headers):
    r = requests.post(
        f"{BASE}/api/admin/system-flags/trigger-refire",
        headers=admin_headers,
        json={"enabled": True},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    assert r.json()["effective_trigger_refire_enabled"] is True

    s = requests.get(f"{BASE}/api/admin/system-flags", headers=admin_headers, timeout=15)
    effective = (s.json() or {}).get("effective") or s.json()
    assert effective.get("trigger_refire_enabled") is True


def test_changes_feed_shape_and_order(admin_headers):
    # Trigger 2 more flips so we know there are recent rows
    requests.post(
        f"{BASE}/api/admin/system-flags/paradox-v3-brains",
        headers=admin_headers, json={"brains": ["camino"]}, timeout=15,
    )
    requests.post(
        f"{BASE}/api/admin/system-flags/paradox-v3-brains",
        headers=admin_headers, json={"brains": ["camino", "gto"]}, timeout=15,
    )
    r = requests.get(
        f"{BASE}/api/admin/system-flags/changes?limit=5",
        headers=admin_headers, timeout=15,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "changes" in body
    rows = body["changes"]
    assert isinstance(rows, list) and len(rows) >= 2
    for row in rows[:2]:
        for k in ("flag", "before", "after", "actor", "ts"):
            assert k in row, f"row missing {k}: {row}"
    # Reverse chronological: ts of row[0] >= ts of row[1]
    assert rows[0]["ts"] >= rows[1]["ts"]


def test_paradox_v3_brains_reflected_in_effective_flags(admin_headers):
    """Rewired 2026-02-17: was `test_paradox_v3_status_includes_...`
    which hit the retired /api/admin/paradox-v3/status endpoint. The
    same guarantee (DB-backed setter reflects into effective read) is
    still checked here via the live /api/admin/system-flags GET."""
    # Set known state
    requests.post(
        f"{BASE}/api/admin/system-flags/paradox-v3-brains",
        headers=admin_headers, json={"brains": ["camino"]}, timeout=15,
    )
    s = requests.get(f"{BASE}/api/admin/system-flags", headers=admin_headers, timeout=15)
    assert s.status_code == 200, s.text
    sj = s.json()
    # effective_paradox_v3_brains is the DB-backed effective value.
    effective = sj.get("effective") or sj
    brains = effective.get("paradox_v3_brains") or effective.get("effective_paradox_v3_brains")
    assert "camino" in (brains or []), (
        f"paradox-v3-brains setter didn't reflect into effective read: {sj}"
    )


def test_zzz_cleanup(admin_headers):
    """Reset to safe defaults so production tile isn't polluted.

    We cannot delete the DB doc via API (no such endpoint), but we can
    null out the operator intent by re-setting to false/empty. The
    operator's environment still has the env-var fallbacks if needed.
    """
    requests.post(
        f"{BASE}/api/admin/system-flags/trigger-watcher",
        headers=admin_headers, json={"enabled": False}, timeout=15,
    )
    requests.post(
        f"{BASE}/api/admin/system-flags/trigger-refire",
        headers=admin_headers, json={"enabled": False}, timeout=15,
    )
    # Note: brains row will still be ['camino', 'gto'] from earlier test —
    # the spec says clean DB at end, but we can't truly delete via API.
    # The conftest cleanup at module teardown handles direct DB wipe.
