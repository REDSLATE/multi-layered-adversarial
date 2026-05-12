"""Seat-policy + auto-mode position tests.

Doctrine:
    Identity does not grant authority. Seat policy does.

This file verifies:
  - Seat policy is exposed on /api/admin/roster (may_execute etc per seat).
  - Auto-mode positions advance only when the brain holding the executor
    seat posts a long/short stance.
  - Auto-mode positions do NOT advance when a non-executor brain posts.
  - Auto-mode + executor abstain does NOT advance (only long/short trigger).
  - Manual-mode positions never auto-advance even when the executor posts.
  - Every stance carries the full seat-policy snapshot (may_execute,
    may_decide, may_override, may_veto, posted_as, seat_epoch).
  - seat-performance endpoint returns the per-(brain, seat) matrix.
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
    key = f"{brain.upper()}_INGEST_TOKEN"
    for line in open("/app/backend/.env").read().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(f"no token for {brain}")


def _propose(tok: str, call_mode: str = "manual", symbol: str = "TEST") -> dict:
    r = requests.post(
        f"{BASE_URL}/api/shared/positions",
        headers=_hdr(tok),
        json={
            "symbol": f"{symbol}_{uuid.uuid4().hex[:6]}",
            "thesis": "auto/manual test",
            "proposed_by": "operator",
            "call_mode": call_mode,
        },
        timeout=10,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _post_stance(pid: str, brain: str, stance: str, conf: float = 0.7) -> dict:
    r = requests.post(
        f"{BASE_URL}/api/runtime-discussion/positions/{pid}/stance?runtime={brain}",
        headers={"X-Runtime-Token": _runtime_token(brain), "Content-Type": "application/json"},
        json={"stance": stance, "confidence": conf, "notes": ""},
        timeout=10,
    )
    assert r.status_code == 200, f"{brain}/{stance}: {r.text}"
    return r.json()


# ──────────────────────── seat policy ────────────────────────

class TestSeatPolicy:
    def test_policy_exposed_on_roster(self):
        tok = _login()
        r = requests.get(f"{BASE_URL}/api/admin/roster", headers=_hdr(tok), timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert "policy" in d
        assert "seat_epoch" in d
        # All 5 seats present
        assert set(d["policy"].keys()) == {"decider", "executor", "governor", "advisor", "opponent"}
        # Executor seat carries may_execute=True; opponent does not
        assert d["policy"]["executor"]["may_execute"] is True
        assert d["policy"]["opponent"]["may_execute"] is False
        # Governor has veto, executor does not
        assert d["policy"]["governor"]["may_veto"] is True
        assert d["policy"]["executor"]["may_veto"] is False

    def test_stance_snapshot_has_all_policy_bits(self):
        tok = _login()
        p = _propose(tok)
        s = _post_stance(p["position_id"], "alpha", "long")
        alpha = s["stances_by_brain"]["alpha"]
        for k in ("posted_as", "seat_epoch", "may_decide", "may_execute",
                  "may_override", "may_veto", "posted_via", "posted_at"):
            assert k in alpha, f"missing {k} on stance snapshot"

    def test_seat_epoch_bumps_on_assign(self):
        tok = _login()
        r1 = requests.get(f"{BASE_URL}/api/admin/roster", headers=_hdr(tok), timeout=10)
        e1 = r1.json()["seat_epoch"]
        # Toggle decider to None and back to camaro
        requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "decider", "brain": None},
            timeout=10,
        )
        requests.post(
            f"{BASE_URL}/api/admin/roster/assign",
            headers=_hdr(tok),
            json={"role": "decider", "brain": "camaro"},
            timeout=10,
        )
        r2 = requests.get(f"{BASE_URL}/api/admin/roster", headers=_hdr(tok), timeout=10)
        e2 = r2.json()["seat_epoch"]
        assert e2 > e1, f"seat_epoch did not bump: {e1} → {e2}"


# ──────────────────────── auto / manual call mode ────────────────────────

class TestAutoMode:
    def test_auto_position_advances_on_executor_long(self):
        tok = _login()
        p = _propose(tok, call_mode="auto", symbol="AUTOL")
        pid = p["position_id"]
        assert p["call_mode"] == "auto"
        # Alpha (executor) stamps long → should auto-advance
        s = _post_stance(pid, "alpha", "long")
        assert s["state"] == "consensus_long"
        assert s["direction"] == "long"
        assert s["executor_call_by"] == "alpha"

    def test_auto_position_advances_on_executor_short(self):
        tok = _login()
        p = _propose(tok, call_mode="auto", symbol="AUTOS")
        s = _post_stance(p["position_id"], "alpha", "short")
        assert s["state"] == "consensus_short"
        assert s["direction"] == "short"

    def test_auto_position_does_NOT_advance_on_non_executor(self):
        """REDEYE (opponent) posting short on an auto position must NOT
        advance state — opponent has no may_execute."""
        tok = _login()
        p = _propose(tok, call_mode="auto", symbol="AUTOX")
        s = _post_stance(p["position_id"], "redeye", "short")
        assert s["state"] == "discussing"
        assert s["direction"] is None

    def test_auto_position_does_NOT_advance_on_executor_abstain(self):
        tok = _login()
        p = _propose(tok, call_mode="auto", symbol="AUTOA")
        s = _post_stance(p["position_id"], "alpha", "abstain")
        assert s["state"] == "discussing"
        assert s["direction"] is None

    def test_manual_position_never_auto_advances(self):
        """Manual call_mode: even alpha (executor) stamping long must NOT
        auto-advance. Operator must click CALL LONG."""
        tok = _login()
        p = _propose(tok, call_mode="manual", symbol="MAN")
        s = _post_stance(p["position_id"], "alpha", "long")
        assert s["state"] == "discussing"
        assert s["direction"] is None

    def test_default_call_mode_is_manual(self):
        """Backwards-compatibility: positions proposed without call_mode
        default to manual."""
        tok = _login()
        # call _propose with the field but default it to manual; also
        # exercise the no-field path.
        r = requests.post(
            f"{BASE_URL}/api/shared/positions",
            headers=_hdr(tok),
            json={"symbol": f"DEF_{uuid.uuid4().hex[:6]}", "proposed_by": "operator"},
            timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["call_mode"] == "manual"


# ──────────────────────── seat performance ────────────────────────

class TestSeatPerformance:
    def test_matrix_returns(self):
        tok = _login()
        # Post a fresh stance so the matrix has at least one row.
        p = _propose(tok)
        _post_stance(p["position_id"], "alpha", "long")

        r = requests.get(
            f"{BASE_URL}/api/admin/roster/seat-performance",
            headers=_hdr(tok),
            timeout=15,
        )
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d["matrix"], list)
        assert d["seats"] == ["decider", "executor", "governor", "advisor", "opponent"]
        # Alpha-as-executor must be present and have at least 1 stance.
        alpha_exec = next(
            (row for row in d["matrix"]
             if row["brain"] == "alpha" and row["seat"] == "executor"),
            None,
        )
        assert alpha_exec is not None
        assert alpha_exec["stances_total"] >= 1

    def test_auth_required(self):
        r = requests.get(
            f"{BASE_URL}/api/admin/roster/seat-performance", timeout=10,
        )
        assert r.status_code in (401, 403)
