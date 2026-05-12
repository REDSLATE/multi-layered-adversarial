"""Position primitive tests.

Covers:
  - propose → list → get (operator path)
  - runtime stance (X-Runtime-Token auth, all 4 brains)
  - operator stance override
  - state machine: proposed → discussing on first stance
  - executor-call advances to consensus_long / consensus_short
  - reject advances to rejected
  - stance posting blocked once position is terminal (409)
  - bad stance value 422
  - JWT required on operator paths
"""
import os
import uuid

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


def _login() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=20,
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def _hdr(t: str) -> dict:
    return {"Authorization": f"Bearer {t}", "Content-Type": "application/json"}


def _runtime_token(brain: str) -> str:
    import re
    line = next(
        (l for l in open("/app/backend/.env").read().splitlines()
         if l.startswith(f"{brain.upper()}_INGEST_TOKEN")),
        None,
    )
    assert line, f"no token for {brain}"
    return re.split("=", line, maxsplit=1)[1].strip().strip('"')


def _propose(tok: str, symbol: str = "TEST") -> dict:
    r = requests.post(
        f"{BASE_URL}/api/shared/positions",
        headers=_hdr(tok),
        json={
            "symbol": f"{symbol}_{uuid.uuid4().hex[:6]}",
            "regime_tag": "trend",
            "thesis": "test",
            "proposed_by": "operator",
        },
        timeout=10,
    )
    assert r.status_code == 200, r.text
    return r.json()


class TestPropose:
    def test_propose_returns_open_state(self):
        tok = _login()
        p = _propose(tok)
        assert p["state"] == "proposed"
        assert p["direction"] is None
        assert p["brains_engaged"] == 0
        assert p["stance_counts"] == {"long": 0, "short": 0, "abstain": 0}

    def test_list_filter_open(self):
        tok = _login()
        _propose(tok)
        r = requests.get(
            f"{BASE_URL}/api/shared/positions?state=open",
            headers=_hdr(tok),
            timeout=10,
        )
        assert r.status_code == 200
        items = r.json()["items"]
        # At least one open position (proposed or discussing)
        assert all(it["state"] in ("proposed", "discussing") for it in items)

    def test_get_404(self):
        tok = _login()
        r = requests.get(
            f"{BASE_URL}/api/shared/positions/{uuid.uuid4()}",
            headers=_hdr(tok),
            timeout=10,
        )
        assert r.status_code == 404


class TestRuntimeStance:
    def test_all_four_brains_can_post(self):
        tok = _login()
        p = _propose(tok)
        pid = p["position_id"]
        results = {}
        plan = {"alpha": "long", "camaro": "long", "chevelle": "abstain", "redeye": "short"}
        for brain, stance in plan.items():
            r = requests.post(
                f"{BASE_URL}/api/runtime-discussion/positions/{pid}/stance?runtime={brain}",
                headers={"X-Runtime-Token": _runtime_token(brain), "Content-Type": "application/json"},
                json={"stance": stance, "confidence": 0.7, "notes": "test"},
                timeout=10,
            )
            assert r.status_code == 200, f"{brain}: {r.text}"
            results[brain] = r.json()
        # Final state should be 'discussing', counts should reflect 4 stances
        final = results["redeye"]
        assert final["state"] == "discussing"
        assert final["stance_counts"]["long"] == 2
        assert final["stance_counts"]["short"] == 1
        assert final["stance_counts"]["abstain"] == 1
        assert final["brains_engaged"] == 4
        # REDEYE's posted_as should be 'opponent' (post-rename)
        redeye_stance = final["stances_by_brain"]["redeye"]
        assert redeye_stance["posted_as"] == "opponent"
        # REDEYE in opponent seat has NO execute authority — verify the
        # policy snapshot stamped at stance-write-time reflects that.
        assert redeye_stance["may_execute"] is False
        assert redeye_stance["may_decide"] is False
        # ALPHA's posted_as should be 'executor' (current seat)
        alpha_stance = final["stances_by_brain"]["alpha"]
        assert alpha_stance["posted_as"] == "executor"
        # Alpha in executor seat HAS execute authority on the snapshot
        # (the bit is operationally inert in Phase 1 — no orders fire —
        # but the contract field must be True so the Phase 2 broker
        # exec-gate can consult it).
        assert alpha_stance["may_execute"] is True
        assert alpha_stance["may_decide"] is True

    def test_runtime_bad_token_401(self):
        tok = _login()
        p = _propose(tok)
        r = requests.post(
            f"{BASE_URL}/api/runtime-discussion/positions/{p['position_id']}/stance?runtime=alpha",
            headers={"X-Runtime-Token": "bogus", "Content-Type": "application/json"},
            json={"stance": "long"},
            timeout=10,
        )
        assert r.status_code == 401

    def test_bad_stance_422(self):
        tok = _login()
        p = _propose(tok)
        r = requests.post(
            f"{BASE_URL}/api/runtime-discussion/positions/{p['position_id']}/stance?runtime=alpha",
            headers={"X-Runtime-Token": _runtime_token("alpha"), "Content-Type": "application/json"},
            json={"stance": "buy"},  # not valid
            timeout=10,
        )
        assert r.status_code == 422


class TestOperatorStance:
    def test_operator_posts_on_behalf(self):
        tok = _login()
        p = _propose(tok)
        r = requests.post(
            f"{BASE_URL}/api/admin/positions/{p['position_id']}/stance",
            headers=_hdr(tok),
            json={"brain": "alpha", "stance": "long", "confidence": 0.8, "notes": "operator override"},
            timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        assert d["state"] == "discussing"
        assert d["stances_by_brain"]["alpha"]["stance"] == "long"
        assert d["stances_by_brain"]["alpha"]["posted_via"] == "operator"


class TestExecutorCall:
    def test_call_long_advances_state(self):
        tok = _login()
        p = _propose(tok)
        r = requests.post(
            f"{BASE_URL}/api/admin/positions/{p['position_id']}/executor-call",
            headers=_hdr(tok),
            json={"direction": "long", "notes": "test"},
            timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        assert d["state"] == "consensus_long"
        assert d["direction"] == "long"
        assert d["executor_call_by"] == "alpha"  # default executor seat

    def test_call_short_advances_state(self):
        tok = _login()
        p = _propose(tok)
        r = requests.post(
            f"{BASE_URL}/api/admin/positions/{p['position_id']}/executor-call",
            headers=_hdr(tok),
            json={"direction": "short"},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["state"] == "consensus_short"

    def test_call_after_terminal_409(self):
        tok = _login()
        p = _propose(tok)
        requests.post(
            f"{BASE_URL}/api/admin/positions/{p['position_id']}/executor-call",
            headers=_hdr(tok),
            json={"direction": "long"},
            timeout=10,
        )
        r = requests.post(
            f"{BASE_URL}/api/admin/positions/{p['position_id']}/executor-call",
            headers=_hdr(tok),
            json={"direction": "short"},
            timeout=10,
        )
        assert r.status_code == 409


class TestReject:
    def test_reject_advances_state(self):
        tok = _login()
        p = _propose(tok)
        r = requests.post(
            f"{BASE_URL}/api/admin/positions/{p['position_id']}/reject",
            headers=_hdr(tok),
            json={"notes": "no thesis"},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["state"] == "rejected"


class TestTerminalLock:
    def test_stance_post_after_terminal_409(self):
        tok = _login()
        p = _propose(tok)
        requests.post(
            f"{BASE_URL}/api/admin/positions/{p['position_id']}/reject",
            headers=_hdr(tok),
            json={"notes": "no thesis"},
            timeout=10,
        )
        r = requests.post(
            f"{BASE_URL}/api/admin/positions/{p['position_id']}/stance",
            headers=_hdr(tok),
            json={"brain": "alpha", "stance": "long"},
            timeout=10,
        )
        assert r.status_code == 409


class TestAuth:
    def test_propose_requires_jwt(self):
        r = requests.post(
            f"{BASE_URL}/api/shared/positions",
            json={"symbol": "X", "proposed_by": "operator"},
            timeout=10,
        )
        assert r.status_code in (401, 403)

    def test_list_requires_jwt(self):
        r = requests.get(f"{BASE_URL}/api/shared/positions", timeout=10)
        assert r.status_code in (401, 403)

    def test_executor_call_requires_jwt(self):
        r = requests.post(
            f"{BASE_URL}/api/admin/positions/{uuid.uuid4()}/executor-call",
            json={"direction": "long"},
            timeout=10,
        )
        assert r.status_code in (401, 403)
