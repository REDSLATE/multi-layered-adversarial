"""Quorum awareness + memory provenance tests.

Quorum: surfaces silent adversarial / governance blindness so a missing
OPPONENT (e.g. REDEYE dies) doesn't quietly dial up risk.

Provenance: stances optionally carry memory_sources + confidence_origin
so future audits can trace memory poisoning / reinforcement loops.
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

class TestQuorum:
    def test_fresh_position_has_all_required_seats_missing(self):
        tok = _login()
        _reset_roster(tok)
        p = _propose(tok)
        q = p["quorum"]
        assert set(q["seats_required"]) == {"executor", "governor", "opponent"}
        assert set(q["seats_missing"]) == {"executor", "governor", "opponent"}
        assert q["adversarial_blindness"] is True
        assert q["governance_blindness"] is True
        assert q["degraded"] is True

    def test_opponent_silent_flags_adversarial_blindness(self):
        tok = _login()
        _reset_roster(tok)
        p = _propose(tok)
        pid = p["position_id"]
        _post(pid, "alpha", stance="long")
        _post(pid, "camaro", stance="long")
        final = _post(pid, "chevelle", stance="abstain")
        q = final["quorum"]
        assert q["adversarial_blindness"] is True
        assert q["governance_blindness"] is False
        assert q["degraded"] is True
        assert q["seats_missing"] == ["opponent"]

    def test_governor_silent_flags_governance_blindness(self):
        tok = _login()
        _reset_roster(tok)
        p = _propose(tok)
        pid = p["position_id"]
        _post(pid, "alpha", stance="long")
        _post(pid, "camaro", stance="long")
        final = _post(pid, "redeye", stance="short")
        q = final["quorum"]
        assert q["governance_blindness"] is True
        assert q["adversarial_blindness"] is False
        assert "governor" in q["seats_missing"]

    def test_full_quorum_clears_all_flags(self):
        tok = _login()
        _reset_roster(tok)
        p = _propose(tok)
        pid = p["position_id"]
        _post(pid, "alpha", stance="long")
        _post(pid, "camaro", stance="long")
        _post(pid, "chevelle", stance="abstain")
        final = _post(pid, "redeye", stance="short")
        q = final["quorum"]
        assert q["adversarial_blindness"] is False
        assert q["governance_blindness"] is False
        assert q["degraded"] is False
        assert q["seats_missing"] == []

    def test_vacant_required_seat_is_surfaced(self):
        tok = _login()
        _reset_roster(tok)
        # Vacate the opponent seat — REDEYE is the only brain eligible
        # for opponent in defaults, so this leaves opponent vacant.
        requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "opponent", "brain": None},
            timeout=10,
        )
        p = _propose(tok)
        q = p["quorum"]
        assert "opponent" in q["vacant_required_seats"]
        assert q["adversarial_blindness"] is True
        # Re-fill so we don't leave the system in a weird state.
        _reset_roster(tok)


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
