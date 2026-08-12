"""Iteration 38: Admin regime + moomoo endpoint smoke tests via public URL."""
import os
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://multi-brain-backbone.preview.emergentagent.com").rstrip("/")
ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PASSWORD = "risedual-admin-2026"


@pytest.fixture(scope="module")
def auth_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text[:200]}"
    return s


# ---------- Regime engine endpoints ----------

def test_regime_state(auth_session):
    r = auth_session.get(f"{BASE_URL}/api/admin/regime/state", timeout=30)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert "lanes" in data
    for lane in ("equity", "crypto"):
        assert lane in data["lanes"], f"missing lane {lane}"
        snap = data["lanes"][lane]
        probs = snap.get("probs") or snap.get("state_probs") or {}
        # tolerate different shape
        if isinstance(probs, dict) and probs:
            total = sum(float(v) for v in probs.values())
            assert 0.9 <= total <= 1.1, f"probs don't sum to 1: {total}"
        timeline = snap.get("timeline") or []
        assert isinstance(timeline, list)
        # 90 entries expected but tolerate small deviation
        assert 60 <= len(timeline) <= 120, f"timeline len={len(timeline)}"
        if timeline:
            entry = timeline[0]
            for k in ("date", "state", "label", "prob"):
                assert k in entry, f"timeline entry missing {k}: {entry}"
        assert "model_version" in snap or "model_version" in data


def test_regime_edge_preview(auth_session):
    r = auth_session.get(f"{BASE_URL}/api/admin/regime/edge-preview", timeout=30)
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d.get("armed") is False, f"regime_edge must be armed=false, got {d.get('armed')}"
    cfg = d.get("config", {})
    assert float(cfg.get("clamp_lo", 0)) == 0.7
    assert float(cfg.get("clamp_hi", 0)) == 1.3
    assert "lanes" in d
    assert "equity" in d["lanes"] and "crypto" in d["lanes"]


def test_regime_brain_matrix(auth_session):
    r = auth_session.get(f"{BASE_URL}/api/admin/regime/brain-matrix", timeout=30)
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d.get("ok") is True
    assert d.get("cluster_adjusted") is True
    assert "stamped_outcomes" in d


def test_regime_setups(auth_session):
    r = auth_session.get(f"{BASE_URL}/api/admin/regime/setups", timeout=30)
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert "rows" in d
    counts = d.get("counts", {})
    assert "active" in counts and "terminated" in counts


def test_regime_model_info(auth_session):
    r = auth_session.get(f"{BASE_URL}/api/admin/regime/model-info", params={"lane": "equity"}, timeout=30)
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    model = d.get("model") or {}
    bic = model.get("bic_report") or d.get("bic_report") or []
    assert bic, "bic_report empty"
    ns = [row.get("n_states") for row in bic if isinstance(row, dict)]
    assert set([3, 4, 5, 6]).issubset(set(ns)), f"bic n_states missing: {ns}"
    states_meta = model.get("states_meta") or d.get("states_meta") or []
    labels = [s.get("label") for s in states_meta if isinstance(s, dict)]
    assert len(labels) == len(set(labels)), f"labels not unique: {labels}"


def test_regime_refresh(auth_session):
    r = auth_session.post(f"{BASE_URL}/api/admin/regime/refresh", json={}, timeout=60)
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d.get("ok") is True


# ---------- MooMoo stream endpoints (OpenD OFFLINE expected) ----------

def test_moomoo_stream_status(auth_session):
    r = auth_session.get(f"{BASE_URL}/api/admin/moomoo/stream/status", timeout=15)
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    stream = d.get("stream") or d
    assert stream.get("running") is True
    assert stream.get("connected") is False
    assert stream.get("last_error") == "waiting_for_opend_config"


def test_moomoo_stream_depth_fallback(auth_session):
    r = auth_session.get(f"{BASE_URL}/api/admin/moomoo/stream/depth/AAPL", timeout=15)
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    depth = d.get("depth", d)
    assert float(depth.get("confirmation", -1)) == 0.5
    assert depth.get("source") == "fallback_no_depth"


# ---------- Regression: other admin endpoints still respond ----------

def test_admin_reconciliation(auth_session):
    r = auth_session.get(f"{BASE_URL}/api/admin/reconciliation", timeout=15)
    assert r.status_code == 200, r.text[:300]


def test_admin_moomoo_status(auth_session):
    r = auth_session.get(f"{BASE_URL}/api/admin/moomoo/status", timeout=15)
    assert r.status_code == 200, r.text[:300]
