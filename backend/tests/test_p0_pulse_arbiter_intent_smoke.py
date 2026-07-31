"""P0 smoke test — iteration 26.

Validates the 2026-07-13 P0 fix: pulse loop now auto-arbitrates and
closes the pulse->arbiter->intent->auto_router path.

Assumptions:
- Tests hit the preview backend at REACT_APP_BACKEND_URL.
- Admin auth via /api/auth/login (creds from /app/memory/test_credentials.md).
- All tests state-safe: any flip (arbiter LIVE, master switch armed) is
  reset in a teardown so preview stays inert.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import pytest
import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://multi-brain-backbone.preview.emergentagent.com",
).rstrip("/")

ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PASSWORD = "risedual-admin-2026"

# ---------- fixtures ----------

@pytest.fixture(scope="module")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def token(api):
    r = api.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=15,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    tok = r.json().get("access_token")
    assert tok and isinstance(tok, str), "no access_token in login body"
    return tok


@pytest.fixture(scope="module")
def auth_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


@pytest.fixture(scope="module", autouse=True)
def _restore_safe_state(auth_headers):
    """After the whole module runs, force arbiter=DISARMED and
    trading enabled=false so preview stays inert."""
    yield
    try:
        requests.post(
            f"{BASE_URL}/api/mc/arbiter/runtime-mode",
            headers=auth_headers,
            json={"mode": "DISARMED", "changed_by": "iter26_smoke_teardown",
                  "reason": "restore_safe_state"},
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        # trading_controls uses /toggle in this repo (not /controls).
        requests.post(
            f"{BASE_URL}/api/admin/trading/toggle",
            headers=auth_headers,
            json={"enabled": False, "reason": "iter26_smoke_teardown"},
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        pass


# ---------- arbiter runtime-mode flip ----------

class TestArbiterRuntimeMode:
    """Tests the read-and-write path for the arbiter runtime mode.

    Note: the repo exposes GET at `/api/mc/arbiter/state` (not
    `/api/mc/arbiter/runtime-mode`). The review request mis-names the
    read endpoint; we test the actual one.
    """

    def test_state_is_readable(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/mc/arbiter/state",
            headers=auth_headers, timeout=15,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "runtime_mode" in body
        assert body["runtime_mode"] in ("LIVE", "DISARMED")

    def test_flip_to_live_and_back(self, auth_headers):
        # LIVE
        r = requests.post(
            f"{BASE_URL}/api/mc/arbiter/runtime-mode",
            headers=auth_headers,
            json={"mode": "LIVE", "changed_by": "p0_smoke",
                  "reason": "p0_smoke"},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("runtime_mode") == "LIVE", body

        # verify GET
        g = requests.get(
            f"{BASE_URL}/api/mc/arbiter/state",
            headers=auth_headers, timeout=15,
        )
        assert g.status_code == 200
        assert g.json()["runtime_mode"] == "LIVE"

        # DISARMED
        r2 = requests.post(
            f"{BASE_URL}/api/mc/arbiter/runtime-mode",
            headers=auth_headers,
            json={"mode": "DISARMED", "changed_by": "p0_smoke",
                  "reason": "p0_smoke_reset"},
            timeout=15,
        )
        assert r2.status_code == 200
        assert r2.json().get("runtime_mode") == "DISARMED"

        g2 = requests.get(
            f"{BASE_URL}/api/mc/arbiter/state",
            headers=auth_headers, timeout=15,
        )
        assert g2.status_code == 200
        assert g2.json()["runtime_mode"] == "DISARMED"


# ---------- per-brain pulse health endpoints ----------

BRAINS = ["barracuda", "hellcat", "gto", "camino"]


class TestPulseHealth:
    """Reviews the pulse health surfaces. Repo does NOT expose
    top-level `/api/mc/pulse-health`, `/regimes`, or `/alignment`.
    Actual endpoints are per-brain paths — tested here.
    """

    @pytest.mark.parametrize("brain", BRAINS)
    def test_per_brain_pulse_health(self, brain, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/mc/pulse-health/{brain}?hours=24",
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200, f"{brain}: {r.status_code} {r.text[:200]}"
        body = r.json()
        assert isinstance(body, dict)
        # canonical shape
        assert body.get("brain") == brain
        # sanity: presence of at least some of the expected keys
        # (participation, distinctness, arbiter_alignment)
        # if the brain has zero snapshots, the endpoint should still
        # return 200 with a well-formed structure.
        keys = set(body.keys())
        assert keys & {"participation", "distinctness",
                       "arbiter_alignment", "no_data_rate",
                       "counts", "hours"}, f"unexpected shape: {keys}"

    @pytest.mark.parametrize("brain", BRAINS)
    def test_per_brain_history(self, brain, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/mc/pulse-health/{brain}/history?limit=5",
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("brain") == brain
        assert "snapshots" in body

    @pytest.mark.parametrize("brain", BRAINS)
    def test_per_brain_by_regime(self, brain, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/mc/pulse-health/{brain}/by-regime?days=7",
            headers=auth_headers, timeout=20,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("brain") == brain
        assert "by_regime" in body


# ---------- e2e-trace ----------

class TestE2ETrace:
    def test_e2e_trace_returns_stages(self, auth_headers):
        # Endpoint accepts query params, not JSON body.
        r = requests.post(
            f"{BASE_URL}/api/mc/pulse-health/e2e-trace"
            "?symbol=AAPL&lane=equity",
            headers=auth_headers, timeout=60,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body, dict)
        assert "stages" in body, f"no stages in trace: {list(body)}"
        assert isinstance(body["stages"], list)
        assert len(body["stages"]) > 0


# ---------- diagnostics + lane execution toggles ----------

class TestDiagnostics:
    def test_diagnostics_lane_execution_present(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/admin/diagnostics",
            headers=auth_headers, timeout=15,
        )
        assert r.status_code == 200
        body = r.json()
        le = body.get("lane_execution") or body.get("body", {}).get("lane_execution")
        # Accept either flat or nested under 'body'
        if le is None:
            le = body.get("body", {}).get("lane_execution")
        assert le is not None, f"lane_execution missing: keys={list(body)[:10]}"
        assert "equity" in le
        assert "crypto" in le
        assert "any_enabled" in le

    def test_no_alpaca_reference(self, auth_headers):
        r = requests.get(
            f"{BASE_URL}/api/admin/diagnostics",
            headers=auth_headers, timeout=15,
        )
        assert r.status_code == 200
        body_txt = r.text.lower()
        assert "alpaca" not in body_txt, (
            f"Diagnostics response mentions 'alpaca'; live-only broker "
            f"isolation broken."
        )


# ---------- brain_runtime cached status ----------

class TestBrainRuntimeStatusCache:
    """Contract: cached status returns latest_ts populated (not None)
    from `brain_runtime_metrics`, and no atlas_partial flag."""

    @pytest.mark.parametrize("brain", BRAINS)
    def test_status_fast_and_hydrated(self, brain, auth_headers):
        t0 = time.time()
        r = requests.get(
            f"{BASE_URL}/api/admin/runtime/{brain}/status",
            headers=auth_headers, timeout=10,
        )
        elapsed = time.time() - t0
        assert r.status_code == 200, r.text
        assert elapsed < 5.0, f"status too slow: {elapsed:.2f}s"
        body = r.json()
        # Response is wrapped in a proxy envelope: intents block is
        # under body["payload"]["intents"].
        payload = body.get("payload") or {}
        assert "intents" in payload, (
            f"payload.intents missing: keys={list(payload)[:10]}"
        )
        intents = payload["intents"]
        src = intents.get("source")
        # atlas_partial can live at either scope — reject truthy.
        assert intents.get("atlas_partial") in (None, False), (
            f"intents.atlas_partial={intents.get('atlas_partial')} for {brain}"
        )
        assert body.get("atlas_partial") in (None, False), (
            f"body.atlas_partial={body.get('atlas_partial')} for {brain}"
        )
        # Hard requirement from the review request:
        assert src == "brain_runtime_metrics", (
            f"{brain}: intents.source={src!r} (expected 'brain_runtime_metrics')"
        )
        # 2026-07-25: latest_ts is 48h-bounded (iteration 39) — a brain
        # idle beyond the window legitimately reports None. Only demand
        # hydration when the window shows activity.
        if intents.get("last_24h"):
            assert intents.get("latest_ts") is not None, (
                f"{brain}: intents.latest_ts is None despite 24h activity"
            )


# ---------- pulse_tick auto_arbitrate signature ----------

class TestPulseTickSignatureLocal:
    """Assert the P0 code-level fix is present in-source.
    Cheap import test — does not require running the pulse."""

    def test_pulse_tick_accepts_auto_arbitrate_kw(self):
        import inspect
        from mc_pulse.pulse import pulse_tick
        sig = inspect.signature(pulse_tick)
        assert "auto_arbitrate" in sig.parameters, sig
        assert sig.parameters["auto_arbitrate"].default is False

    def test_pulse_worker_reads_env_toggles(self):
        import mc_pulse.pulse_worker as pw
        src = inspect.getsource(pw)
        assert "MC_PULSE_COMPARE_ONLY" in src
        assert "MC_PULSE_AUTO_ARBITRATE" in src
        assert "get_runtime_mode" in src

import inspect  # noqa: E402  (used inside class)


# ---------- End-to-end pulse->arbiter->intent path ----------

class TestE2EPulseToIntent:
    """The core P0 verification: after flipping arbiter to LIVE
    (leaves master switch OFF so no broker traffic), verify that
    the running pulse loop emits at least one NEW shared_intents doc
    with `evidence.arbitrated_by == 'mc_arbiter'` within ~60s.

    This proves the pulse->arbiter->intent link is closed. The
    downstream link (auto_router->broker) is intentionally not armed
    here to keep preview inert.

    Cleanup: arbiter is forced back to DISARMED regardless of outcome.
    """

    def _list_arbiter_intents(self, auth_headers):
        # sort=newest: the default "conviction" sort hides fresh
        # intents once 100+ higher-confidence rows exist in the
        # retention window (observed 2026-07-31: top-100 floor 0.75,
        # fresh SELLs at 0.44 → invisible forever → false failure).
        r = requests.get(
            f"{BASE_URL}/api/intents?limit=100&include_disabled_lanes=true&sort=newest",
            headers=auth_headers, timeout=20,
        )
        if r.status_code != 200:
            return []
        items = r.json().get("items", [])
        # Only "real" ones — skip synthetic e2e-trace symbols so a
        # concurrent trace test doesn't contaminate the baseline.
        out = []
        for it in items:
            ev = it.get("evidence") or {}
            if ev.get("arbitrated_by") != "mc_arbiter":
                continue
            sym = (it.get("symbol") or "").upper()
            if sym.startswith("E2ETRC") or sym.startswith("TEST_"):
                continue
            out.append(it)
        return out

    def test_pulse_emits_intent_when_arbiter_live(self, auth_headers):
        baseline = self._list_arbiter_intents(auth_headers)
        baseline_ids = {it.get("intent_id") for it in baseline}

        # 2026-07-25: the dynamic-risk-sizer selection layer enforces a
        # 0.55 confidence floor + 0.50 score floor inside the arbiter's
        # winner path. Live brain confidence can sit below that, which
        # is a CORRECT no_eligible_brain outcome — this test asserts
        # the pulse→arbiter→intent WIRING, so relax the floors for the
        # duration and restore afterwards.
        r = requests.post(
            f"{BASE_URL}/api/admin/risk-sizer/policy",
            headers=auth_headers,
            json={"selection": {"min_confidence": 0.0, "min_score": 0.0}},
            timeout=15,
        )
        assert r.status_code == 200, r.text

        # Flip arbiter to LIVE
        r = requests.post(
            f"{BASE_URL}/api/mc/arbiter/runtime-mode",
            headers=auth_headers,
            json={"mode": "LIVE", "changed_by": "p0_smoke_e2e",
                  "reason": "p0_smoke_e2e_pulse_intent_path"},
            timeout=15,
        )
        assert r.status_code == 200, r.text
        assert r.json()["runtime_mode"] == "LIVE"

        try:
            # Pulse cadence is 15s; Camino runs every 30s. All-brain
            # ticks fire every 30s. Give it up to 90s to see at least
            # one arbitrated_by=mc_arbiter intent land.
            deadline = time.time() + 90
            new_ids: set[str] = set()
            while time.time() < deadline:
                current = self._list_arbiter_intents(auth_headers)
                current_ids = {it.get("intent_id") for it in current}
                new_ids = current_ids - baseline_ids
                if new_ids:
                    break
                time.sleep(10)
            assert new_ids, (
                "no new mc_arbiter intent appeared in shared_intents "
                "within 90s of flipping arbiter to LIVE — the P0 "
                "pulse->arbiter->intent path is BROKEN."
            )
        finally:
            requests.post(
                f"{BASE_URL}/api/admin/risk-sizer/policy",
                headers=auth_headers,
                json={"selection": {"min_confidence": 0.55, "min_score": 0.50}},
                timeout=15,
            )
            # ALWAYS reset arbiter to DISARMED, even on assertion fail.
            requests.post(
                f"{BASE_URL}/api/mc/arbiter/runtime-mode",
                headers=auth_headers,
                json={"mode": "DISARMED", "changed_by": "p0_smoke_e2e",
                      "reason": "p0_smoke_e2e_teardown"},
                timeout=15,
            )
            g = requests.get(
                f"{BASE_URL}/api/mc/arbiter/state",
                headers=auth_headers, timeout=15,
            )
            assert g.json().get("runtime_mode") == "DISARMED", (
                "FAILED to restore arbiter to DISARMED — preview left LIVE!"
            )

