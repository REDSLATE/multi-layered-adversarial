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
            "long_advisor": None,
            "short_advisor": "redeye",
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
            json={"role": "short_advisor", "brain": None},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["assignments"]["short_advisor"] is None

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
            "long_advisor": None,
            "short_advisor": "redeye",
        }

    def test_audit_log_captures_changes(self):
        tok = _login()
        _reset(tok)
        # Make one swap so a fresh entry definitely exists.
        requests.post(
            f"{BASE_URL}/api/admin/roster/swap",
            headers=_hdr(tok),
            json={"role_a": "decider", "role_b": "short_advisor"},
            timeout=10,
        )
        r = requests.get(f"{BASE_URL}/api/admin/roster/audit", headers=_hdr(tok), timeout=10)
        assert r.status_code == 200
        items = r.json()["items"]
        assert any(it["action"] == "swap" for it in items[:10])

    def test_auth_required(self):
        r = requests.get(f"{BASE_URL}/api/admin/roster", timeout=10)
        assert r.status_code in (401, 403)


class TestEligibility:
    def test_default_matrix(self):
        tok = _login()
        requests.post(f"{BASE_URL}/api/admin/roster/eligibility/reset", headers=_hdr(tok), timeout=10)
        r = requests.get(f"{BASE_URL}/api/admin/roster/eligibility", headers=_hdr(tok), timeout=10)
        assert r.status_code == 200
        m = r.json()["matrix"]
        # Chevelle = governor only
        assert m["chevelle"]["governor"] is True
        assert m["chevelle"]["decider"] is False
        assert m["chevelle"]["executor"] is False
        assert m["chevelle"]["long_advisor"] is False
        assert m["chevelle"]["short_advisor"] is False
        # REDEYE = short_advisor only (was sidecar-advisor, now full short seat)
        assert m["redeye"]["short_advisor"] is True
        assert m["redeye"]["long_advisor"] is False
        assert m["redeye"]["decider"] is False
        # Alpha/Camaro = decider/executor/long_advisor, NOT governor or short_advisor
        for b in ("alpha", "camaro"):
            assert m[b]["governor"] is False
            assert m[b]["short_advisor"] is False
            assert m[b]["decider"] is True
            assert m[b]["executor"] is True
            assert m[b]["long_advisor"] is True

    def test_assign_blocked_by_default_matrix(self):
        tok = _login()
        _reset(tok)
        requests.post(f"{BASE_URL}/api/admin/roster/eligibility/reset", headers=_hdr(tok), timeout=10)
        # REDEYE→decider is blocked by default
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "decider", "brain": "redeye"},
            timeout=10,
        )
        assert r.status_code == 400
        assert "not eligible" in r.text.lower()

    def test_toggle_eligibility_then_assign(self):
        tok = _login()
        _reset(tok)
        requests.post(f"{BASE_URL}/api/admin/roster/eligibility/reset", headers=_hdr(tok), timeout=10)
        # Flip the switch
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/eligibility",
            headers=_hdr(tok),
            json={"brain": "redeye", "role": "decider", "allowed": True},
            timeout=10,
        )
        assert r.status_code == 200
        # Now the assign succeeds
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "decider", "brain": "redeye"},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["assignments"]["decider"] == "redeye"
        # Restore
        _reset(tok)
        requests.post(f"{BASE_URL}/api/admin/roster/eligibility/reset", headers=_hdr(tok), timeout=10)

    def test_cannot_disallow_current_occupant(self):
        tok = _login()
        _reset(tok)
        requests.post(f"{BASE_URL}/api/admin/roster/eligibility/reset", headers=_hdr(tok), timeout=10)
        # Camaro currently holds decider (default). Try to mark decider
        # disallowed for camaro → 400.
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/eligibility",
            headers=_hdr(tok),
            json={"brain": "camaro", "role": "decider", "allowed": False},
            timeout=10,
        )
        assert r.status_code == 400
        assert "currently occupy" in r.text.lower()

    def test_swap_blocked_by_eligibility(self):
        tok = _login()
        _reset(tok)
        requests.post(f"{BASE_URL}/api/admin/roster/eligibility/reset", headers=_hdr(tok), timeout=10)
        # Swap governor ↔ decider would put chevelle into decider
        # (chevelle is NOT eligible for decider by default).
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/swap",
            headers=_hdr(tok),
            json={"role_a": "decider", "role_b": "governor"},
            timeout=10,
        )
        assert r.status_code == 400


class TestTenure:
    def test_tenure_response_shape(self):
        tok = _login()
        _reset(tok)
        r = requests.get(f"{BASE_URL}/api/admin/roster/tenure", headers=_hdr(tok), timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert "per_role" in d
        assert {row["role"] for row in d["per_role"]} == {"decider", "executor", "governor", "long_advisor", "short_advisor"}
        assert "average_tenure_days" in d
        assert "churn_state" in d
        assert d["churn_state"] in ("LOW", "MEDIUM", "HIGH")
        assert "doctrine_invariant" in d
        # Invariant must mention execution
        assert "execution" in d["doctrine_invariant"].lower()

    def test_tenure_resets_on_swap(self):
        tok = _login()
        _reset(tok)
        # Move alpha into governor would fail (eligibility), so swap two
        # eligible roles instead — decider ↔ executor.
        before = requests.get(f"{BASE_URL}/api/admin/roster/tenure", headers=_hdr(tok), timeout=10).json()
        before_decider_days = next(r["days_in_role"] for r in before["per_role"] if r["role"] == "decider")

        requests.post(
            f"{BASE_URL}/api/admin/roster/swap",
            headers=_hdr(tok),
            json={"role_a": "decider", "role_b": "executor"},
            timeout=10,
        )
        after = requests.get(f"{BASE_URL}/api/admin/roster/tenure", headers=_hdr(tok), timeout=10).json()
        after_decider_days = next(r["days_in_role"] for r in after["per_role"] if r["role"] == "decider")
        # New occupant just entered → days_in_role is now ~0 (definitely
        # less than whatever it was before)
        if before_decider_days is not None:
            assert after_decider_days < before_decider_days or after_decider_days < 0.01
        _reset(tok)


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
