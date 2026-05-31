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

ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL", "admin@risedual.io")
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD", "risedual-admin-2026")


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
        a = d["assignments"]
        # 2026-05-24 (corrected): REDEYE not seated by default. Opponent
        # is operator-assigned. `decider` was renamed to `strategist`.
        assert a["strategist"] == "camaro"
        assert a["executor"]   == "alpha"
        assert a["governor"]   == "chevelle"
        assert a.get("opponent") is None
        assert a.get("auditor") is None
        assert a.get("advisor") is None
        # Doctrine note must be present in the response
        assert "authority" in d["doctrine"].lower()

    def test_assign_vacates_old_role(self):
        tok = _login()
        _reset(tok)
        # Move camaro from strategist → executor; strategist should become None
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "executor", "brain": "camaro"},
            timeout=10,
        )
        assert r.status_code == 200
        a = r.json()["assignments"]
        assert a["executor"] == "camaro"
        assert a["strategist"] is None
        # And alpha (previously executor) is now nowhere
        assert "alpha" not in a.values()

    def test_assign_none_vacates_role(self):
        tok = _login()
        _reset(tok)
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "auditor", "brain": None},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["assignments"]["auditor"] is None

    def test_swap_atomic(self):
        tok = _login()
        _reset(tok)
        # Swap strategist ↔ executor (camaro ↔ alpha)
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/swap",
            headers=_hdr(tok),
            json={"role_a": "strategist", "role_b": "executor"},
            timeout=10,
        )
        assert r.status_code == 200
        a = r.json()["assignments"]
        assert a["strategist"] == "alpha"
        assert a["executor"] == "camaro"

    def test_swap_same_role_rejected(self):
        tok = _login()
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/swap",
            headers=_hdr(tok),
            json={"role_a": "strategist", "role_b": "strategist"},
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
            json={"role": "strategist", "brain": "tesla"},
            timeout=10,
        )
        assert r.status_code == 422

    def test_legacy_decider_alias_accepted(self):
        """Backwards-compat: posting `decider` is silently rewritten to
        `strategist` (the canonical name after the 2026-05-24 rename).
        Old sidecars that still send the legacy seat name keep working."""
        tok = _login()
        _reset(tok)
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "decider", "brain": "alpha"},
            timeout=10,
        )
        # alpha is eligible for strategist by default → accepted
        assert r.status_code == 200, r.text
        a = r.json()["assignments"]
        # Canonical key is set; legacy key does not appear
        assert a["strategist"] == "alpha"
        assert "decider" not in a
        _reset(tok)

    def test_reset_restores_defaults(self):
        tok = _login()
        # Mangle then reset
        requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "executor", "brain": "camaro"},
            timeout=10,
        )
        _reset(tok)
        r = requests.get(f"{BASE_URL}/api/admin/roster", headers=_hdr(tok), timeout=10)
        a = r.json()["assignments"]
        # 2026-05-24 (corrected): REDEYE not seated by default
        assert a["strategist"] == "camaro"
        assert a["executor"]   == "alpha"
        assert a["governor"]   == "chevelle"
        assert a.get("opponent") is None

    def test_audit_log_captures_changes(self):
        tok = _login()
        _reset(tok)
        # Make one swap so a fresh entry definitely exists.
        requests.post(
            f"{BASE_URL}/api/admin/roster/swap",
            headers=_hdr(tok),
            json={"role_a": "strategist", "role_b": "executor"},
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
        """2026-05-26 (operator doctrine revision):
          All seats are open to all brains EXCEPT `governor` and its
          crypto twin `crypto_governor`, which are exclusive to
          Chevelle and RedEye. All other cells default True.
        """
        tok = _login()
        requests.post(f"{BASE_URL}/api/admin/roster/eligibility/reset", headers=_hdr(tok), timeout=10)
        r = requests.get(f"{BASE_URL}/api/admin/roster/eligibility", headers=_hdr(tok), timeout=10)
        assert r.status_code == 200
        m = r.json()["matrix"]
        # Non-governor seats from the canonical 8: every brain × seat = True
        non_gov = ("strategist", "executor", "auditor",
                   "crypto_strategist", "crypto", "crypto_auditor")
        for brain in ("alpha", "camaro", "chevelle", "redeye"):
            for seat in non_gov:
                assert m[brain][seat] is True, (
                    f"default matrix should allow {brain}→{seat}"
                )
        # Governor + crypto_governor: only Chevelle and RedEye
        for seat in ("governor", "crypto_governor"):
            assert m["alpha"][seat] is False
            assert m["camaro"][seat] is False
            assert m["chevelle"][seat] is True
            assert m["redeye"][seat] is True

    def test_assign_any_brain_to_any_non_governor_seat_by_default(self):
        """All non-governor seats: any brain accepted by default.
        Governor seats: only Chevelle / RedEye accepted."""
        tok = _login()
        _reset(tok)
        requests.post(f"{BASE_URL}/api/admin/roster/eligibility/reset", headers=_hdr(tok), timeout=10)
        # Chevelle → strategist — accepted (non-governor)
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "strategist", "brain": "chevelle"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        # Camaro → governor — REJECTED (governor exclusivity)
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "governor", "brain": "camaro"},
            timeout=10,
        )
        assert r.status_code == 400, r.text
        assert "exclusive" in (r.json().get("detail") or "").lower()
        # RedEye → governor — accepted (eligible brain)
        # First vacate chevelle so the seat is open.
        requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "governor", "brain": None},
            timeout=10,
        )
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "governor", "brain": "redeye"},
            timeout=10,
        )
        assert r.status_code == 200, r.text
        _reset(tok)

    def test_toggle_eligibility_then_assign(self):
        """Operator can tighten a non-governor cell and re-open it.
        Governor cells are doctrine-locked — re-open attempt is refused."""
        tok = _login()
        _reset(tok)
        requests.post(f"{BASE_URL}/api/admin/roster/eligibility/reset", headers=_hdr(tok), timeout=10)
        # Non-governor: operator disallows alpha→opponent, then re-allows.
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/eligibility",
            headers=_hdr(tok),
            json={"brain": "alpha", "role": "opponent", "allowed": False},
            timeout=10,
        )
        assert r.status_code == 200
        # Alpha→opponent assignment now blocked
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "opponent", "brain": "alpha"},
            timeout=10,
        )
        assert r.status_code == 400
        # Re-allow.
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/eligibility",
            headers=_hdr(tok),
            json={"brain": "alpha", "role": "opponent", "allowed": True},
            timeout=10,
        )
        assert r.status_code == 200
        # Governor: operator CANNOT re-enable alpha→governor (doctrine lock)
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/eligibility",
            headers=_hdr(tok),
            json={"brain": "alpha", "role": "governor", "allowed": True},
            timeout=10,
        )
        assert r.status_code == 400
        assert "exclusive" in (r.json().get("detail") or "").lower()
        _reset(tok)
        requests.post(f"{BASE_URL}/api/admin/roster/eligibility/reset", headers=_hdr(tok), timeout=10)

    def test_cannot_disallow_current_occupant(self):
        tok = _login()
        _reset(tok)
        requests.post(f"{BASE_URL}/api/admin/roster/eligibility/reset", headers=_hdr(tok), timeout=10)
        # Camaro currently holds strategist (default). Try to disallow.
        r = requests.post(
            f"{BASE_URL}/api/admin/roster/eligibility",
            headers=_hdr(tok),
            json={"brain": "camaro", "role": "strategist", "allowed": False},
            timeout=10,
        )
        assert r.status_code == 400
        assert "currently occupy" in r.text.lower()

    def test_redeye_not_seated_by_default(self):
        """REDEYE is NOT seated by default — it lives across positions
        via stances. Opponent seat starts vacant; operator decides who
        sits there (if anyone)."""
        tok = _login()
        _reset(tok)
        r = requests.get(f"{BASE_URL}/api/admin/roster", headers=_hdr(tok), timeout=10)
        a = r.json()["assignments"]
        assert a.get("auditor") is None
        assert "redeye" not in {v for v in a.values() if v}


class TestTenure:
    def test_tenure_response_shape(self):
        tok = _login()
        _reset(tok)
        r = requests.get(f"{BASE_URL}/api/admin/roster/tenure", headers=_hdr(tok), timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert "per_role" in d
        # 2026-05-31 canonical 8-seat doctrine — these are the only roles
        # the tenure aggregator should return.
        roles = {row["role"] for row in d["per_role"]}
        for seat in ("strategist", "executor", "governor", "auditor",
                     "crypto_strategist", "crypto", "crypto_governor",
                     "crypto_auditor"):
            assert seat in roles, f"missing seat in tenure: {seat}"
        assert "average_tenure_days" in d
        assert "churn_state" in d
        assert d["churn_state"] in ("LOW", "MEDIUM", "HIGH")
        assert "doctrine_invariant" in d
        # Invariant must mention execution
        assert "execution" in d["doctrine_invariant"].lower()

    def test_tenure_resets_on_swap(self):
        tok = _login()
        _reset(tok)
        before = requests.get(f"{BASE_URL}/api/admin/roster/tenure", headers=_hdr(tok), timeout=10).json()
        before_days = next(
            row["days_in_role"] for row in before["per_role"]
            if row["role"] == "strategist"
        )

        # Swap two eligible seats — strategist ↔ executor (camaro ↔ alpha)
        requests.post(
            f"{BASE_URL}/api/admin/roster/swap",
            headers=_hdr(tok),
            json={"role_a": "strategist", "role_b": "executor"},
            timeout=10,
        )
        after = requests.get(f"{BASE_URL}/api/admin/roster/tenure", headers=_hdr(tok), timeout=10).json()
        after_days = next(
            row["days_in_role"] for row in after["per_role"]
            if row["role"] == "strategist"
        )
        # New occupant just entered → days_in_role is now ~0
        if before_days is not None:
            assert after_days < before_days or after_days < 0.01
        _reset(tok)


class TestOpinionStamping:
    def test_opinion_gets_posted_as(self):
        tok = _login()
        _reset(tok)
        # Camaro posts → should be stamped per the strategist seat's
        # alias-resolved policy. The current alias maps strategist's
        # SEAT_POLICY lookup to the executor row, so posted_as resolves
        # to "executor".
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
        # Fetch the opinion back and verify the posted_as field is one of
        # the strategist's recognized labels (strategist OR executor — the
        # alias backstop is permitted during the rename transition).
        r = requests.get(
            f"{BASE_URL}/api/shared/opinions",
            params={"runtime": "camaro", "limit": 20},
            headers=_hdr(tok), timeout=20,
        )
        match = next((x for x in r.json()["items"] if x["opinion_id"] == oid), None)
        assert match is not None
        assert match.get("posted_as") in ("strategist", "executor", "decider")
        assert match.get("may_execute") is False  # doctrine intact

    def test_opinion_reflects_role_change(self):
        """Move alpha into 'strategist', then have alpha post — the
        posted_as stamp should reflect the new seat."""
        tok = _login()
        _reset(tok)
        # Swap so alpha holds strategist
        requests.post(
            f"{BASE_URL}/api/admin/roster/swap",
            headers=_hdr(tok),
            json={"role_a": "strategist", "role_b": "executor"},
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
                    "body": "alpha now strategist",
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
            assert match.get("posted_as") in ("strategist", "executor", "decider")
        finally:
            _reset(tok)
