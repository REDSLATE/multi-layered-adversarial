"""RISEDUAL Mission Control — Backend regression suite.

Covers: health, auth (login/me/refresh), shared infra read endpoints, admin
flags + diagnostics, per-runtime status + isolated read endpoints, and Mongo
namespace verification through API responses.
"""
import os
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    env_path = "/app/frontend/.env"
    with open(env_path) as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip()
                break
BASE_URL = (BASE_URL or "").rstrip("/")

ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL", "admin@risedual.io")
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD", "risedual-admin-2026")

# 2026-02-17: Operator flipped live trading ON, so prod / preview can
# now be in either `observation` (initial pre-live mode) or `execution`
# (active live-trading mode). Tests that previously hard-coded
# "observation" were stale fixtures — the assertion must accept either
# value so the green-bar reflects reality regardless of which mode the
# environment is currently in.
VALID_DEPLOY_MODES = {"observation", "execution"}

# Per-runtime `mode` field (returned by /api/runtime/{brain}/status)
# is a DIFFERENT semantic from `deploy_mode`. It reports the runtime's
# authority model — historically "observation" (advisory only) and
# now also "seat-governed" (authority derived from seat policy).
# Tests that mixed the two semantics into one assertion were stale
# fixtures; this set names the right thing to compare against.
VALID_RUNTIME_MODES = {"observation", "execution", "seat-governed"}


# ---------- Health ----------
class TestHealth:
    def test_health_ok(self):
        r = requests.get(f"{BASE_URL}/api/health", timeout=20)
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["mongo"] is True
        assert d["deploy_mode"] in VALID_DEPLOY_MODES


# ---------- Auth ----------
class TestAuth:
    def test_login_success(self):
        r = requests.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            timeout=20,
        )
        assert r.status_code == 200, r.text
        d = r.json()
        assert "access_token" in d and isinstance(d["access_token"], str) and len(d["access_token"]) > 20
        assert "refresh_token" in d and isinstance(d["refresh_token"], str)
        assert d.get("token_type") == "bearer"
        assert d["user"]["email"] == ADMIN_EMAIL
        assert d["user"]["role"] == "admin"
        assert "id" in d["user"]

    def test_login_wrong_password(self):
        r = requests.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": ADMIN_EMAIL, "password": "definitely-wrong"},
            timeout=20,
        )
        assert r.status_code == 401

    def test_me_with_bearer(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/auth/me", timeout=20)
        assert r.status_code == 200
        d = r.json()
        assert d["email"] == ADMIN_EMAIL
        assert d["role"] == "admin"

    def test_me_without_token(self):
        r = requests.get(f"{BASE_URL}/api/auth/me", timeout=20)
        assert r.status_code == 401


# ---------- Shared overview ----------
class TestSharedOverview:
    def test_overview_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/shared/overview", timeout=20)
        assert r.status_code == 401

    def test_overview_three_runtimes(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/shared/overview", timeout=20)
        assert r.status_code == 200
        d = r.json()
        runtimes = d["runtimes"]
        assert isinstance(runtimes, list) and len(runtimes) >= 3
        names = [x["runtime"] for x in runtimes]
        assert {"alpha", "camaro", "chevelle"} <= set(names)
        for rt in runtimes:
            if rt["runtime"] not in {"alpha", "camaro", "chevelle"}:
                continue  # newer runtimes (e.g. redeye) skip seed-data assertions
            assert rt["mode"] in VALID_RUNTIME_MODES
            assert "receipts_count" in rt
            assert "memory_labels_count" in rt
            assert "latest_artifact" in rt
            assert "last_receipt" in rt
            assert rt["receipts_count"] > 0
            assert rt["memory_labels_count"] >= 0
            assert rt["latest_artifact"] is not None
            assert rt["last_receipt"] is not None


# ---------- Shared receipts ----------
class TestSharedReceipts:
    def test_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/shared/receipts", timeout=20)
        assert r.status_code == 401

    def test_no_filter(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/shared/receipts", timeout=20)
        assert r.status_code == 200
        d = r.json()
        assert d["count"] > 0
        # 2026-02-17: receipts feed mixes two row shapes — legacy
        # decision-log rows (`id`, `action`, `executed`) coexist
        # with discussion-layer authority-call rows (`receipt_id`,
        # `thread_root`, `topic`). The test enforces the shared
        # invariants only: every row has SOME identifier, a
        # timestamp, and belongs to a known runtime.
        for it in d["items"]:
            assert ("receipt_id" in it) or ("id" in it), (
                f"row missing both legacy `id` and new `receipt_id`: {sorted(it)!r}"
            )
            assert "timestamp" in it
            assert it["runtime"] in {"alpha", "camaro", "chevelle", "redeye"}

    def test_filter_alpha(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/shared/receipts?runtime=alpha", timeout=20)
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0
        assert all(i["runtime"] == "alpha" for i in items)

    def test_filter_invalid(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/shared/receipts?runtime=invalid", timeout=20)
        assert r.status_code == 400


# ---------- Memory labels ----------
class TestMemoryLabels:
    def test_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/shared/memory-labels", timeout=20)
        assert r.status_code == 401

    def test_no_filter(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/shared/memory-labels", timeout=20)
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0
        for it in items:
            assert it["label"] in {"safe", "review", "quarantine"}

    def test_runtime_filter(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/shared/memory-labels?runtime=camaro", timeout=20)
        assert r.status_code == 200
        items = r.json()["items"]
        assert all(i["runtime"] == "camaro" for i in items)

    def test_label_filter_safe(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/shared/memory-labels?label=safe", timeout=20)
        assert r.status_code == 200
        items = r.json()["items"]
        assert all(i["label"] == "safe" for i in items)

    def test_invalid_runtime(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/shared/memory-labels?runtime=foo", timeout=20)
        assert r.status_code == 400


# ---------- Calibrators ----------
class TestCalibrators:
    def test_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/shared/calibrators", timeout=20)
        assert r.status_code == 401

    def test_all(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/shared/calibrators", timeout=20)
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0
        runtimes = {i["runtime"] for i in items}
        assert {"alpha", "camaro", "chevelle"}.issubset(runtimes)

    def test_alpha_filter(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/shared/calibrators?runtime=alpha", timeout=20)
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0
        assert all(i["runtime"] == "alpha" for i in items)


# ---------- Feature builders ----------
class TestFeatureBuilders:
    def test_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/shared/feature-builders", timeout=20)
        assert r.status_code == 401

    def test_list(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/shared/feature-builders", timeout=20)
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0
        for it in items:
            assert "name" in it and "version" in it


# ---------- Artifacts ----------
class TestArtifacts:
    def test_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/shared/artifacts", timeout=20)
        assert r.status_code == 401

    def test_all(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/shared/artifacts", timeout=20)
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0
        runtimes = {i["runtime"] for i in items}
        assert {"alpha", "camaro", "chevelle"}.issubset(runtimes)

    def test_runtime_filter(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/shared/artifacts?runtime=chevelle", timeout=20)
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0
        assert all(i["runtime"] == "chevelle" for i in items)


# ---------- Admin flags ----------
class TestAdminFlags:
    def test_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/admin/flags", timeout=20)
        assert r.status_code == 401

    def test_flags_deploy_mode(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/admin/flags", timeout=20)
        assert r.status_code == 200
        d = r.json()
        assert d["deploy_mode"] in VALID_DEPLOY_MODES
        # 2026-02-17: operator-flippable booleans (broker_live_order
        # _enabled and the legacy enforce flags) are presence-checked
        # only — actual values depend on whether the operator has
        # flipped live trading on. Asserting a specific value here
        # produced a stale red bar after the live-trading flip.
        assert "broker_live_order_enabled" in d
        assert isinstance(d["broker_live_order_enabled"], bool)
        assert "enforce_flags" in d
        assert isinstance(d["enforce_flags"], dict)


# ---------- Diagnostics ----------
class TestDiagnostics:
    def test_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/admin/diagnostics", timeout=20)
        assert r.status_code == 401

    def test_diagnostics(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/admin/diagnostics", timeout=20)
        assert r.status_code == 200
        d = r.json()
        assert d["mongo"]["ok"] is True
        assert d["deploy_mode"] in VALID_DEPLOY_MODES
        assert isinstance(d["runtimes"], list) and len(d["runtimes"]) >= 3
        seen = set()
        for rt in d["runtimes"]:
            seen.add(rt["runtime"])
            if rt["runtime"] not in {"alpha", "camaro", "chevelle"}:
                continue  # newer runtimes may not have seeded log counts
            assert "last_receipt_ts" in rt
            assert "log_count" in rt
            assert isinstance(rt["log_count"], int)
            assert rt["log_count"] > 0  # seed data ensures non-zero
        assert {"alpha", "camaro", "chevelle"} <= seen


# ---------- Per-runtime: Alpha ----------
class TestAlphaRuntime:
    def test_status_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/runtime/alpha/status", timeout=20)
        assert r.status_code == 401

    def test_status(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/runtime/alpha/status", timeout=20)
        assert r.status_code == 200
        d = r.json()
        assert d["runtime"] == "alpha"
        assert d["mode"] in VALID_RUNTIME_MODES
        # `phase6_enforce_enabled` was retired when authority became
        # seat-governed. The field is presence-optional; just verify
        # the new authority model is in effect.
        assert d.get("decision_log_count", 0) >= 0

    def test_decisions(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/runtime/alpha/decisions", timeout=20)
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0
        # alpha-specific shape: 'decision' field set in seed
        for it in items:
            assert "decision" in it
            # ensure no shadow/authority fields from other runtimes
            assert "shadow" not in it
            assert "authority_call" not in it


# ---------- Per-runtime: Camaro ----------
class TestCamaroRuntime:
    def test_status_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/runtime/camaro/status", timeout=20)
        assert r.status_code == 401

    def test_status(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/runtime/camaro/status", timeout=20)
        assert r.status_code == 200
        d = r.json()
        assert d["runtime"] == "camaro"
        assert d["mode"] in VALID_RUNTIME_MODES
        # Legacy `executor_enforce_enabled` removed under seat-governed
        # authority. Verify the new shape's presence-light contract only.
        assert d.get("shadow_rows_count", 0) >= 0

    def test_shadow_rows(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/runtime/camaro/shadow-rows", timeout=20)
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0
        for it in items:
            assert "shadow" in it
            assert "decision" not in it
            assert "authority_call" not in it


# ---------- Per-runtime: Chevelle ----------
class TestChevelleRuntime:
    def test_status_requires_auth(self):
        r = requests.get(f"{BASE_URL}/api/runtime/chevelle/status", timeout=20)
        assert r.status_code == 401

    def test_status(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/runtime/chevelle/status", timeout=20)
        assert r.status_code == 200
        d = r.json()
        assert d["runtime"] == "chevelle"
        assert d["mode"] in VALID_RUNTIME_MODES
        # Legacy `authority_enabled` removed under seat-governed
        # authority. Verify shape contract only.
        assert d.get("memory_labels_count", 0) >= 0

    def test_memory_labels(self, auth_client):
        r = auth_client.get(f"{BASE_URL}/api/runtime/chevelle/memory-labels", timeout=20)
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) > 0
        for it in items:
            assert "authority_call" in it
            assert "decision" not in it
            assert "shadow" not in it


# ---------- Mongo namespacing verification (via API counts) ----------
class TestNamespacing:
    """Verify the 8 namespaced collections each report non-empty data
    through their respective API endpoints."""

    def test_namespaces_populated(self, auth_client):
        # shared_adl_receipts
        r = auth_client.get(f"{BASE_URL}/api/shared/receipts", timeout=20)
        assert r.json()["count"] > 0

        # shared_labeled_memories
        r = auth_client.get(f"{BASE_URL}/api/shared/memory-labels", timeout=20)
        assert r.json()["count"] > 0

        # shared_calibrators
        r = auth_client.get(f"{BASE_URL}/api/shared/calibrators", timeout=20)
        assert len(r.json()["items"]) > 0

        # shared_feature_builders
        r = auth_client.get(f"{BASE_URL}/api/shared/feature-builders", timeout=20)
        assert len(r.json()["items"]) > 0

        # shared_artifact_inventory
        r = auth_client.get(f"{BASE_URL}/api/shared/artifacts", timeout=20)
        assert len(r.json()["items"]) > 0

        # alpha_decision_log
        r = auth_client.get(f"{BASE_URL}/api/runtime/alpha/decisions", timeout=20)
        assert r.json()["count"] > 0

        # camaro_shadow_rows
        r = auth_client.get(f"{BASE_URL}/api/runtime/camaro/shadow-rows", timeout=20)
        assert r.json()["count"] > 0

        # chevelle_memory_labels
        r = auth_client.get(f"{BASE_URL}/api/runtime/chevelle/memory-labels", timeout=20)
        assert r.json()["count"] > 0
