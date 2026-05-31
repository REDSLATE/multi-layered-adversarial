"""Memory provenance tests.

Stance ingestion optionally carries `memory_sources` + `confidence_origin`
so future audits can trace memory poisoning / reinforcement loops.

2026-05-31: The TestQuorum class that previously lived in this module
was deleted — it asserted an early 3-required-seat shape (executor +
governor + opponent) that was a scaffolding hiccup. The canonical IP
doctrine pins exactly 8 seats; new quorum tests will be written when
operator pins lane-filtering behavior.
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


def _token(brain: str) -> str:
    for line in open("/app/backend/.env").read().splitlines():
        if line.startswith(f"{brain.upper()}_INGEST_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"no token for {brain}")


def _propose(tok: str, call_mode: str = "manual") -> dict:
    r = requests.post(
        f"{BASE_URL}/api/shared/positions",
        headers=_hdr(tok),
        json={
            "symbol": f"QP_{uuid.uuid4().hex[:6]}",
            "thesis": "quorum/provenance test",
            "proposed_by": "operator",
            "call_mode": call_mode,
        },
        timeout=10,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _post(pid: str, brain: str, **payload) -> dict:
    body = {"stance": "long", "confidence": 0.7}
    body.update(payload)
    r = requests.post(
        f"{BASE_URL}/api/runtime-discussion/positions/{pid}/stance?runtime={brain}",
        headers={"X-Runtime-Token": _token(brain), "Content-Type": "application/json"},
        json=body,
        timeout=10,
    )
    assert r.status_code == 200, f"{brain}: {r.text}"
    return r.json()


def _reset_roster(tok: str) -> None:
    requests.post(f"{BASE_URL}/api/admin/roster/reset", headers=_hdr(tok), timeout=10)


# ──────────────────────── quorum ────────────────────────
#
# 2026-05-31 — The TestQuorum class that lived here asserted an early
# 3-required-seat shape (executor + governor + opponent) that was a
# scaffolding hiccup, not part of the canonical IP doctrine. The IP
# pins exactly 8 seats (4 equity + 4 crypto). Those obsolete tests were
# deleted per operator decision; they were testing a shape MC was
# never supposed to have.
#
# Future doctrinal quorum tests should:
#   - assert against `shared.seat_policy.CANONICAL_SEATS` (the 8-seat IP boundary)
#   - decide lane-filtering: do equity positions require crypto-seat
#     stances or are they filtered? (Open doctrinal question — pin
#     before writing tests against the answer.)


# ──────────────────────── memory provenance ────────────────────────


class TestProvenance:
    def test_stance_persists_memory_sources_and_origin(self):
        tok = _login()
        _reset_roster(tok)
        p = _propose(tok)
        d = _post(
            p["position_id"], "alpha", stance="long", confidence=0.78,
            memory_sources=["macro_regime_2022", "volatility_cluster_041"],
            confidence_origin={"model": 0.71, "memory": 0.12, "contradiction_penalty": -0.09},
        )
        s = d["stances_by_brain"]["alpha"]
        assert s["memory_sources"] == ["macro_regime_2022", "volatility_cluster_041"]
        assert s["confidence_origin"] == {
            "model": 0.71, "memory": 0.12, "contradiction_penalty": -0.09,
        }

    def test_provenance_is_optional(self):
        """Brain sidecars that don't yet report provenance must still
        be able to post stances."""
        tok = _login()
        _reset_roster(tok)
        p = _propose(tok)
        d = _post(p["position_id"], "alpha", stance="long")
        s = d["stances_by_brain"]["alpha"]
        assert s["memory_sources"] == []
        assert s["confidence_origin"] == {}

    def test_confidence_origin_out_of_range_rejected(self):
        tok = _login()
        _reset_roster(tok)
        p = _propose(tok)
        r = requests.post(
            f"{BASE_URL}/api/runtime-discussion/positions/{p['position_id']}/stance?runtime=alpha",
            headers={"X-Runtime-Token": _token("alpha"), "Content-Type": "application/json"},
            json={"stance": "long", "confidence_origin": {"oops": 2.5}},
            timeout=10,
        )
        assert r.status_code == 422
        assert "must be in" in r.text or "-1" in r.text

    def test_too_many_memory_sources_rejected(self):
        tok = _login()
        _reset_roster(tok)
        p = _propose(tok)
        r = requests.post(
            f"{BASE_URL}/api/runtime-discussion/positions/{p['position_id']}/stance?runtime=alpha",
            headers={"X-Runtime-Token": _token("alpha"), "Content-Type": "application/json"},
            json={"stance": "long", "memory_sources": [f"src_{i}" for i in range(40)]},
            timeout=10,
        )
        # Schema cap is 32 sources
        assert r.status_code == 422

    def test_too_many_origin_components_rejected(self):
        tok = _login()
        _reset_roster(tok)
        p = _propose(tok)
        r = requests.post(
            f"{BASE_URL}/api/runtime-discussion/positions/{p['position_id']}/stance?runtime=alpha",
            headers={"X-Runtime-Token": _token("alpha"), "Content-Type": "application/json"},
            json={"stance": "long",
                  "confidence_origin": {f"c{i}": 0.1 for i in range(20)}},
            timeout=10,
        )
        # Cap is 12 components
        assert r.status_code == 422

    def test_operator_path_also_supports_provenance(self):
        tok = _login()
        _reset_roster(tok)
        p = _propose(tok)
        r = requests.post(
            f"{BASE_URL}/api/admin/positions/{p['position_id']}/stance",
            headers=_hdr(tok),
            json={"brain": "alpha", "stance": "long",
                  "memory_sources": ["op_src_1"],
                  "confidence_origin": {"manual_override": 0.5}},
            timeout=10,
        )
        assert r.status_code == 200
        s = r.json()["stances_by_brain"]["alpha"]
        assert s["memory_sources"] == ["op_src_1"]
        assert s["confidence_origin"] == {"manual_override": 0.5}
