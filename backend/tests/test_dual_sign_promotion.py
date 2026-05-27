"""Single-sign promotion regression (2026-05-26 doctrine update).

This file was originally `test_dual_sign_promotion.py` enforcing the
dual-sign rule for `primary` elevation. Operator confirmed solo-operator
deployment makes dual-sign security theater — all tiers now single-sign.

Doctrine pin:
    Every ladder rung, INCLUDING `primary`, finalises on a single
    countersign from any admin operator. Readiness gate (Patent J)
    still required to PASS. Audit chain still records signer + ts + note.

Filename retained so the doctrine archaeology is visible — anyone
finding this in git history sees: "we used to have dual-sign here,
then collapsed it on 2026-05-26."
"""
import os
import uuid
from datetime import datetime, timezone

import pytest
import requests
from pymongo import MongoClient

# --- env ---
BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip()
                break
BASE_URL = (BASE_URL or "").rstrip("/")


def _read_env(path: str) -> dict:
    out: dict = {}
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


BACKEND_ENV = _read_env("/app/backend/.env")
MONGO_URL = BACKEND_ENV.get("MONGO_URL") or os.environ.get("MONGO_URL")
DB_NAME = BACKEND_ENV.get("DB_NAME") or os.environ.get("DB_NAME")

ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL", "admin@risedual.io")
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD", "risedual-admin-2026")


# ---------- Fixtures ----------

@pytest.fixture(scope="module")
def mongo():
    assert MONGO_URL and DB_NAME, "MONGO_URL / DB_NAME required"
    c = MongoClient(MONGO_URL)
    yield c[DB_NAME]
    c.close()


def _login(email: str, password: str) -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": email, "password": password},
        timeout=20,
    )
    assert r.status_code == 200, f"login {email} failed: {r.status_code} {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="module")
def token_a():
    return _login(ADMIN_EMAIL, ADMIN_PASSWORD)


def _hdr(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_proposal(mongo, *, target: str, readiness_passed: bool,
                   runtime: str | None = None):
    """Insert a synthetic proposal directly. Returns (proposal_id, runtime)."""
    rt = runtime or f"test-rt-{uuid.uuid4().hex[:8]}"
    pid = str(uuid.uuid4())
    doc = {
        "proposal_id": pid,
        "runtime": rt,
        "from_state": "co_trader" if target == "primary" else "observer",
        "target_authority": target,
        "readiness": {
            "runtime": rt, "target_authority": target,
            "evaluated_at": _now_iso(),
            "passed": readiness_passed,
            "checks": [{"name": "synthetic", "pass": readiness_passed,
                        "observed": True, "threshold": True}],
            "artifact_id": None, "thresholds": {},
            "note": "synthetic test fixture",
        },
        "artifact_id": None,
        "status": "pending",
        # Doctrine 2026-05-26: every tier requires exactly 1 signature.
        "required_signatures": 1,
        "signers": [],
        "created_at": _now_iso(),
        "created_by": "test-fixture",
        "decided_at": None, "decided_by": None, "decision_note": None,
    }
    mongo.shared_promotion_proposals.insert_one(doc)
    if target == "primary":
        mongo.shared_authority_state.update_one(
            {"runtime": rt},
            {"$set": {
                "runtime": rt,
                "authority_state": "co_trader",
                "history": [{"to_state": "co_trader", "from_state": None,
                             "at": _now_iso(), "via": "test_fixture",
                             "operator": None, "proposal_id": None}],
                "created_at": _now_iso(),
            }},
            upsert=True,
        )
    return pid, rt


def _cleanup(mongo, pid: str, rt: str):
    mongo.shared_promotion_proposals.delete_many({"proposal_id": pid})
    if rt.startswith("test-rt-"):
        mongo.shared_authority_state.delete_many({"runtime": rt})


# ---------- Tests ----------

class TestSingleSignPrimary:
    """Primary tier MUST elevate on a single countersign from any admin."""

    def test_primary_single_sign_elevates_immediately(self, mongo, token_a):
        pid, rt = _make_proposal(mongo, target="primary", readiness_passed=True)
        try:
            r = requests.post(
                f"{BASE_URL}/api/admin/promotion/{pid}/countersign",
                json={"note": "single sign — solo-op doctrine"},
                headers=_hdr(token_a), timeout=20,
            )
            assert r.status_code == 200, r.text
            d = r.json()
            # The dual-sign awaiting field is now ABSENT (no longer applicable).
            assert d.get("awaiting_more_signatures") is None
            assert d["from_state"] == "co_trader"
            assert d["to_state"] == "primary"
            assert d["signed"] == 1
            assert d["required"] == 1

            doc = mongo.shared_promotion_proposals.find_one({"proposal_id": pid})
            assert doc["status"] == "approved"
            assert len(doc["signers"]) == 1
            assert doc["signers"][0]["operator"] == ADMIN_EMAIL

            st = mongo.shared_authority_state.find_one({"runtime": rt})
            assert st["authority_state"] == "primary"
            last = st["history"][-1]
            assert last["to_state"] == "primary"
            assert last["signers"] == [ADMIN_EMAIL]
        finally:
            _cleanup(mongo, pid, rt)

    def test_primary_failed_readiness_still_blocks(self, mongo, token_a):
        """Readiness gate is doctrine-mandatory — single-sign collapse
        does NOT relax the technical bar."""
        pid, rt = _make_proposal(mongo, target="primary", readiness_passed=False)
        try:
            r = requests.post(
                f"{BASE_URL}/api/admin/promotion/{pid}/countersign",
                json={"note": "should fail"}, headers=_hdr(token_a), timeout=20,
            )
            assert r.status_code == 412, r.text
            doc = mongo.shared_promotion_proposals.find_one({"proposal_id": pid})
            assert doc["status"] == "pending"
            assert doc["signers"] == []
        finally:
            _cleanup(mongo, pid, rt)


class TestSingleSignNonPrimary:
    def test_non_primary_single_sign_elevates_immediately(self, mongo, token_a):
        pid, rt = _make_proposal(mongo, target="challenger", readiness_passed=True)
        try:
            r = requests.post(
                f"{BASE_URL}/api/admin/promotion/{pid}/countersign",
                json={"note": "single sign elevates"}, headers=_hdr(token_a), timeout=20,
            )
            assert r.status_code == 200, r.text
            d = r.json()
            assert d["from_state"] == "observer"
            assert d["to_state"] == "challenger"
            assert d["signed"] == 1 and d["required"] == 1
            doc = mongo.shared_promotion_proposals.find_one({"proposal_id": pid})
            assert doc["status"] == "approved"
            st = mongo.shared_authority_state.find_one({"runtime": rt})
            assert st["authority_state"] == "challenger"
        finally:
            _cleanup(mongo, pid, rt)


class TestProposeRequiredSignatures:
    """The propose endpoint must always set required_signatures=1 now."""

    def test_propose_via_api_required_signatures_always_1(self, mongo, token_a):
        # Primary
        pid, rt = _make_proposal(mongo, target="primary", readiness_passed=True)
        try:
            r = requests.get(
                f"{BASE_URL}/api/admin/promotion/proposals?limit=200",
                headers=_hdr(token_a), timeout=20,
            )
            mine = next((p for p in r.json()["items"] if p["proposal_id"] == pid), None)
            assert mine is not None
            assert mine["required_signatures"] == 1
        finally:
            _cleanup(mongo, pid, rt)
        # Non-primary
        pid, rt = _make_proposal(mongo, target="advisor", readiness_passed=True)
        try:
            r = requests.get(
                f"{BASE_URL}/api/admin/promotion/proposals?limit=200",
                headers=_hdr(token_a), timeout=20,
            )
            mine = next((p for p in r.json()["items"] if p["proposal_id"] == pid), None)
            assert mine is not None
            assert mine["required_signatures"] == 1
        finally:
            _cleanup(mongo, pid, rt)


class TestLegacyAwaitingSecondSignBackCompat:
    """If any pre-doctrine-change proposal sits in `awaiting_second_sign`
    on prod (mid-flight at the time of the change), it MUST finalize on
    the next countersign — not stay stuck forever."""

    def test_legacy_awaiting_second_sign_finalizes_on_one_more_sign(
        self, mongo, token_a,
    ):
        pid, rt = _make_proposal(mongo, target="primary", readiness_passed=True)
        # Simulate legacy mid-flight state: already had one signer, parked.
        mongo.shared_promotion_proposals.update_one(
            {"proposal_id": pid},
            {"$set": {
                "status": "awaiting_second_sign",
                "signers": [{"operator": "legacy-op-a@risedual.io",
                             "at": _now_iso(), "note": "pre-doctrine"}],
                "required_signatures": 2,  # Legacy field; the route ignores it now.
            }},
        )
        try:
            r = requests.post(
                f"{BASE_URL}/api/admin/promotion/{pid}/countersign",
                json={"note": "finalising legacy awaiting row"},
                headers=_hdr(token_a), timeout=20,
            )
            assert r.status_code == 200, r.text
            d = r.json()
            assert d["to_state"] == "primary"
            doc = mongo.shared_promotion_proposals.find_one({"proposal_id": pid})
            assert doc["status"] == "approved"
            # Both signers preserved in audit trail.
            sig_emails = {s["operator"] for s in doc["signers"]}
            assert "legacy-op-a@risedual.io" in sig_emails
            assert ADMIN_EMAIL in sig_emails
        finally:
            _cleanup(mongo, pid, rt)
