"""Live sustained-load probe for /api/admin/runtime/{brain}/status
(iteration 22, 2026-07-09 P0 cached-metrics rollout).

Doctrine (operator directive):
    "The old unbounded shared_intents scan would time out or degrade
     after 5-10 hits. The cached micro-doc path should stay sub-2s
     under sustained polling load."

This suite:
  1. Authenticates as the admin (from /app/memory/test_credentials.md).
  2. For each of the 4 brains (camino, barracuda, hellcat, gto):
       * asserts a single /status returns 200 within 3s
       * asserts payload.intents.source == 'brain_runtime_metrics'
       * asserts payload.intents.atlas_partial == False
       * asserts numeric last_1h / last_24h / by_action / latest_ts
  3. Hammers /status/camino 20 times sequentially; every call must
     return within 2s (proves the cached-doc read stays fast under
     dashboard-poll load).
"""
from __future__ import annotations

import os
import time

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # Fallback for pytest runs where the frontend env isn't sourced.
    try:
        with open("/app/frontend/.env") as _f:
            for _ln in _f:
                if _ln.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = _ln.split("=", 1)[1].strip().rstrip("/")
                    break
    except Exception:
        pass

ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PASSWORD = "risedual-admin-2026"
BRAINS = ("camino", "barracuda", "hellcat", "gto")


@pytest.fixture(scope="module")
def admin_token() -> str:
    """Log in as the operator admin, return the JWT access token."""
    assert BASE_URL, "REACT_APP_BACKEND_URL not configured"
    resp = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=10,
    )
    assert resp.status_code == 200, f"admin login failed: {resp.status_code} {resp.text[:200]}"
    data = resp.json()
    token = data.get("access_token") or data.get("token")
    assert token, f"no access_token in login response: {list(data.keys())}"
    return token


@pytest.fixture(scope="module")
def auth_headers(admin_token) -> dict:
    return {"Authorization": f"Bearer {admin_token}"}


# ── Per-brain shape + speed check ─────────────────────────────────

@pytest.mark.parametrize("brain", BRAINS)
def test_status_returns_cached_source_and_shape(brain, auth_headers):
    """/status must respond in <3s, be sourced from brain_runtime_metrics,
    and populate the intents block with numeric windows + non-null
    latest_ts / by_action."""
    t0 = time.time()
    resp = requests.get(
        f"{BASE_URL}/api/admin/runtime/{brain}/status",
        headers=auth_headers,
        timeout=5,
    )
    elapsed = time.time() - t0
    assert resp.status_code == 200, (
        f"{brain} /status returned {resp.status_code}: {resp.text[:200]}"
    )
    assert elapsed < 3.0, f"{brain} /status took {elapsed:.2f}s (>3s)"

    body = resp.json()
    assert body.get("ok") is True, f"{brain} status ok=False: {body}"
    payload = body.get("payload") or {}
    intents = payload.get("intents") or {}
    assert intents.get("source") == "brain_runtime_metrics", (
        f"{brain} intents.source={intents.get('source')!r}, expected "
        f"'brain_runtime_metrics' — old shared_intents scan may still be live"
    )
    assert intents.get("atlas_partial") is False, (
        f"{brain} atlas_partial={intents.get('atlas_partial')!r} — cached "
        f"doc read must succeed for all 4 brains"
    )
    # Numeric windows.
    for k in ("last_1h", "last_24h"):
        val = intents.get(k)
        assert isinstance(val, int), (
            f"{brain} intents.{k}={val!r} not int — refresh_windows "
            f"did not populate the cached doc"
        )
        assert val >= 0
    # by_action non-null dict.
    ba = intents.get("by_action")
    assert isinstance(ba, dict), f"{brain} by_action not dict: {ba!r}"
    # latest_ts non-null.
    assert intents.get("latest_ts") is not None, (
        f"{brain} latest_ts is null — brain has never emitted OR the "
        f"cached doc read is falling back to runner memory path"
    )


# ── Sustained-load probe: 20 sequential hits, all <2s ─────────────

def test_status_sustained_load_camino(auth_headers):
    """Hit camino/status 20 times back-to-back. Each call must return
    in <2s (the old unbounded shared_intents scan would degrade after
    5-10 hits — this proves the cached-doc read stays cheap)."""
    url = f"{BASE_URL}/api/admin/runtime/camino/status"
    timings: list[float] = []
    for i in range(20):
        t0 = time.time()
        resp = requests.get(url, headers=auth_headers, timeout=5)
        elapsed = time.time() - t0
        timings.append(elapsed)
        assert resp.status_code == 200, (
            f"iter {i}: {resp.status_code} {resp.text[:200]}"
        )
        assert elapsed < 2.0, (
            f"iter {i}: /status took {elapsed:.2f}s (>2s) — "
            f"cached-doc path is regressing under load"
        )
        body = resp.json()
        assert body.get("ok") is True
        src = ((body.get("payload") or {}).get("intents") or {}).get("source")
        assert src == "brain_runtime_metrics", (
            f"iter {i}: source={src!r} — fell out of cached path mid-load"
        )
    p_avg = sum(timings) / len(timings)
    p_max = max(timings)
    p_min = min(timings)
    print(
        f"\nsustained-load timings over 20 hits: min={p_min:.3f}s "
        f"avg={p_avg:.3f}s max={p_max:.3f}s"
    )


# ── 500-error smoke on all four brains + a bogus one ──────────────

@pytest.mark.parametrize("brain", BRAINS + ("bogus-brain",))
def test_status_no_500(brain, auth_headers):
    """No brain — real or bogus — may 500. 401/403/404 are acceptable,
    500 is a code bug."""
    resp = requests.get(
        f"{BASE_URL}/api/admin/runtime/{brain}/status",
        headers=auth_headers,
        timeout=5,
    )
    assert resp.status_code != 500, (
        f"{brain} /status returned 500: {resp.text[:300]}"
    )
    # bogus brain must be a clean 404, not a 200-with-error-body.
    if brain == "bogus-brain":
        assert resp.status_code in (400, 404), (
            f"bogus brain got {resp.status_code}, expected 404"
        )
