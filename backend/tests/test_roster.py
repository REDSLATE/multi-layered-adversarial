"""Brain Roster — backend regression tests.

Verifies:
  - Default assignment is created on first GET.
  - Assign a brain to a new role correctly vacates their old role.
  - Assign with brain=None vacates a role.
  - Swap atomically exchanges two roles.
  - Swap validation rejects role_a == role_b (422).
  - Bad role / bad brain rejected (422).
  - Reset restores defaults.
  - Audit log captures every change.
  - Opinions get stamped with `posted_as` from the current roster.
  - Roster never grants execution: stored opinions still have
    `may_execute=False` and the doctrine field stays in the GET response.
"""
import os
import time

import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip()
                break
BASE_URL = (BASE_URL or "").rstrip("/")

ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PASSWORD = "risedual-admin-2026"


def _env(key: str) -> str:
    with open("/app/backend/.env") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"{key} missing")


CAMARO_TOKEN = _env("CAMARO_INGEST_TOKEN")
ALPHA_TOKEN = _env("ALPHA_INGEST_TOKEN")


def _login() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=20,
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _hdr(t: str) -> dict:
    return {"Authorization": f"Bearer {t}", "Content-Type": "application/json"}


def _reset(tok: str):
    requests.post(f"{BASE_URL}/api/admin/roster/reset", headers=_hdr(tok), timeout=10)


class TestRosterBasics:
    def test_get_returns_defaults(self):
        tok = _login()
        _reset(tok)
        r = requests.get(f"{BASE_URL}/api/admin/roster", headers=_hdr(tok), timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d["assignments"] == {
            "decider": "camaro",
            "executor": "alpha",
            "governor": "chevelle",
            "advisor": "redeye",
        }
        # Doctrine note must be present in the response
        assert "may_execute" in d["doctrine"]

    def test_assign_vacates_old_role(self):
        tok = _login()
        _reset(tok)
        # Move camaro from decider → executor; decider should become None
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "executor", "brain": "camaro"},
            timeout=10,
        )
        assert r.status_code == 200
        a = r.json()["assignments"]
        assert a["executor"] == "camaro"
        assert a["decider"] is None
        # And alpha (previously executor) is now nowhere
        assert "alpha" not in a.values()

    def test_assign_none_vacates_role(self):
        tok = _login()
        _reset(tok)
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "advisor", "brain": None},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["assignments"]["advisor"] is None

    def test_swap_atomic(self):
        tok = _login()
        _reset(tok)
        # Swap decider ↔ executor (camaro ↔ alpha)
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/swap",
            headers=_hdr(tok),
            json={"role_a": "decider", "role_b": "executor"},
            timeout=10,
        )
        assert r.status_code == 200
        a = r.json()["assignments"]
        assert a["decider"] == "alpha"
        assert a["executor"] == "camaro"

    def test_swap_same_role_rejected(self):
        tok = _login()
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/swap",
            headers=_hdr(tok),
            json={"role_a": "decider", "role_b": "decider"},
            timeout=10,
        )
        assert r.status_code == 422

    def test_assign_bad_role_rejected(self):
        tok = _login()
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "puppet_master", "brain": "alpha"},
            timeout=10,
        )
        assert r.status_code == 422

    def test_assign_bad_brain_rejected(self):
        tok = _login()
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "decider", "brain": "tesla"},
            timeout=10,
        )
        assert r.status_code == 422

    def test_reset_restores_defaults(self):
        tok = _login()
        # Mangle then reset
        requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "decider", "brain": "redeye"},
            timeout=10,
        )
        _reset(tok)
        r = requests.get(f"{BASE_URL}/api/admin/roster", headers=_hdr(tok), timeout=10)
        assert r.json()["assignments"] == {
            "decider": "camaro",
            "executor": "alpha",
            "governor": "chevelle",
            "advisor": "redeye",
        }

    def test_audit_log_captures_changes(self):
        tok = _login()
        _reset(tok)
        # Make one swap so a fresh entry definitely exists.
        requests.post(
            f"{BASE_URL}/api/admin/roster/swap",
            headers=_hdr(tok),
            json={"role_a": "decider", "role_b": "advisor"},
            timeout=10,
        )
        r = requests.get(f"{BASE_URL}/api/admin/roster/audit", headers=_hdr(tok), timeout=10)
        assert r.status_code == 200
        items = r.json()["items"]
        assert any(it["action"] == "swap" for it in items[:10])

    def test_auth_required(self):
        r = requests.get(f"{BASE_URL}/api/admin/roster", timeout=10)
        assert r.status_code in (401, 403)


class TestOpinionStamping:
    def test_opinion_gets_posted_as(self):
        tok = _login()
        _reset(tok)
        # Camaro posts → should be stamped as "decider"
        suffix = int(time.time())
        r = requests.post(
            f"{BASE_URL}/api/ingest/opinion",
            headers={"X-Runtime-Token": CAMARO_TOKEN, "Content-Type": "application/json"},
            json={
                "runtime": "camaro",
                "topic": f"symbol:ROSTER{suffix}",
                "stance": "observation",
                "body": "stamp test",
                "confidence": 0.4,
            },
            timeout=20,
        )
        assert r.status_code == 200
        oid = r.json()["opinion_id"]
        # Fetch the opinion back and verify posted_as field
        r = requests.get(
            f"{BASE_URL}/api/shared/opinions",
            params={"runtime": "camaro", "limit": 20},
            headers=_hdr(tok), timeout=20,
        )
        match = next((x for x in r.json()["items"] if x["opinion_id"] == oid), None)
        assert match is not None
        assert match.get("posted_as") == "decider"
        assert match.get("may_execute") is False  # doctrine intact

    def test_opinion_reflects_role_change(self):
        """Move alpha into 'decider', then have alpha post → posted_as should be decider."""
        tok = _login()
        _reset(tok)
        # Swap so alpha is now decider
        requests.post(
            f"{BASE_URL}/api/admin/roster/swap",
            headers=_hdr(tok),
            json={"role_a": "decider", "role_b": "executor"},
            timeout=10,
        )
        try:
            suffix = int(time.time())
            r = requests.post(
                f"{BASE_URL}/api/ingest/opinion",
                headers={"X-Runtime-Token": ALPHA_TOKEN, "Content-Type": "application/json"},
                json={
                    "runtime": "alpha",
                    "topic": f"symbol:STAMP{suffix}",
                    "stance": "long",
                    "body": "alpha now decides",
                    "confidence": 0.6,
                },
                timeout=20,
            )
            assert r.status_code == 200
            oid = r.json()["opinion_id"]
            r = requests.get(
                f"{BASE_URL}/api/shared/opinions",
                params={"runtime": "alpha", "limit": 20},
                headers=_hdr(tok), timeout=20,
            )
            match = next((x for x in r.json()["items"] if x["opinion_id"] == oid), None)
            assert match is not None
            assert match.get("posted_as") == "decider"
        finally:
            _reset(tok)
