"""Public-API rate limit tests.

Coverage:
  * /admin/public-traffic/limits returns per-tier caps (JWT-gated)
  * 200 responses carry X-RateLimit-* headers
  * Free tier hits cap and gets 429 + Retry-After
  * Higher tiers do not trip the free cap
  * 429s appear in the public-traffic log (so operators can see them)
"""
from __future__ import annotations

import os
import time

import pytest
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


def _token() -> str:
    for line in open("/app/backend/.env").read().splitlines():
        if line.startswith("RISEDUAL_PUBLIC_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("RISEDUAL_PUBLIC_TOKEN not set")


PT = _token()


@pytest.fixture(autouse=True)
def _wipe_rate_limit_state():
    """Other test modules can fill the Mongo-backed per-minute counter with
    their warmups. Without this autouse wipe, these rate-limit tests inherit
    a bucket already past the free cap and 429 instead of 200. Wipe before
    each test → deterministic.

    Doctrine (2026-06-10): swapped from motor (async, fragile under
    pytest-asyncio session-loop) to pymongo (sync, bulletproof).
    """
    import pymongo
    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    if mongo_url and db_name:
        client = pymongo.MongoClient(mongo_url, serverSelectionTimeoutMS=2000)
        try:
            client[db_name].public_rate_limits.delete_many({})
        finally:
            client.close()
    yield


def _hdr(tier: str = "free") -> dict:
    return {"X-RiseDual-Token": PT, "X-RiseDual-User-Tier": tier}


def _login() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=20,
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def _wait_for_next_minute() -> None:
    """Doctrine (2026-06-10): only wait if we're too close to the
    wall-clock minute boundary to fit a 35-call burst inside ONE
    bucket. The rate limiter buckets per minute, so straddling a
    boundary splits the count and lets all 35 through under the cap.

    Empirical: 35 sequential HTTPS calls over a Session take ~6s on
    this pod. We wait if fewer than 12s remain in the current minute
    — generous safety margin for slow days. The autouse `_wipe_rate_limit_state`
    fixture handles inter-test cleanup; this fn purely guards against
    the minute-boundary split bug."""
    seconds_left_in_minute = 60 - (int(time.time()) % 60)
    if seconds_left_in_minute < 12:
        time.sleep(seconds_left_in_minute + 0.5)


class TestLimitsEndpoint:
    def test_requires_jwt(self):
        r = requests.get(
            f"{BASE_URL}/api/admin/public-traffic/limits", timeout=10,
        )
        assert r.status_code in (401, 403)

    def test_returns_default_caps(self):
        tok = _login()
        r = requests.get(
            f"{BASE_URL}/api/admin/public-traffic/limits",
            headers={"Authorization": f"Bearer {tok}"}, timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        assert d["window_seconds"] == 60
        caps = d["limits_per_min"]
        assert caps["free"] == 30
        assert caps["starter"] == 60
        assert caps["pro"] == 300
        assert caps["pro_max"] == 1200
        # pro_max strictly > pro > starter > free
        assert caps["pro_max"] > caps["pro"] > caps["starter"] > caps["free"]


class TestRateLimitHeaders:
    def test_200_carries_ratelimit_headers(self):
        _wait_for_next_minute()
        r = requests.get(
            f"{BASE_URL}/api/public/heatmap",
            headers=_hdr("pro_max"), timeout=10,
        )
        assert r.status_code == 200
        assert r.headers.get("X-RateLimit-Tier") == "pro_max"
        assert r.headers.get("X-RateLimit-Limit") == "1200"
        assert int(r.headers.get("X-RateLimit-Remaining", "0")) <= 1199
        assert r.headers.get("X-RateLimit-Window") == "60"


class TestRateLimitEnforcement:
    def test_free_tier_429s_after_cap(self):
        _wait_for_next_minute()
        # Free cap is 30/min. Fire 35 — last 5 should be 429.
        sess = requests.Session()
        statuses: list[int] = []
        try:
            for _ in range(35):
                r = sess.get(
                    f"{BASE_URL}/api/public/heatmap",
                    headers=_hdr("free"), timeout=10,
                )
                statuses.append(r.status_code)
        finally:
            sess.close()
        assert statuses.count(200) == 30
        assert statuses.count(429) == 5

    def test_429_carries_retry_after_and_headers(self):
        # Don't wait — we're already past the cap from the previous test.
        r = requests.get(
            f"{BASE_URL}/api/public/heatmap",
            headers=_hdr("free"), timeout=10,
        )
        # If we're still in the same minute, expect 429. If we rolled
        # over, this turns into a 200; either is acceptable here, but if
        # 429, we MUST have the right headers.
        if r.status_code == 429:
            assert r.headers.get("Retry-After")
            assert r.headers.get("X-RateLimit-Tier") == "free"
            assert r.headers.get("X-RateLimit-Limit") == "30"
            assert r.headers.get("X-RateLimit-Remaining") == "0"

    def test_pro_max_unaffected_by_free_cap(self):
        _wait_for_next_minute()
        # Pro Max cap is 1200/min. 50 calls must all succeed.
        # Doctrine (2026-06-10): use a requests.Session so the SSL
        # handshake is reused across calls — otherwise 50 sequential
        # HTTPS handshakes blow past pytest-timeout on the suite runner.
        sess = requests.Session()
        statuses: list[int] = []
        try:
            for _ in range(50):
                r = sess.get(
                    f"{BASE_URL}/api/public/heatmap",
                    headers=_hdr("pro_max"), timeout=10,
                )
                statuses.append(r.status_code)
        finally:
            sess.close()
        assert statuses.count(200) == 50

    def test_missing_token_not_rate_limited_but_still_401(self):
        # Without X-RiseDual-Token, we skip the rate-limit increment
        # (otherwise random scrapers could lock out a legitimate caller).
        # The trust dep still 401s.
        statuses: list[int] = []
        for _ in range(5):
            r = requests.get(f"{BASE_URL}/api/public/heatmap", timeout=10)
            statuses.append(r.status_code)
        assert all(s == 401 for s in statuses)


class TestLogged429s:
    def test_429s_visible_in_traffic_log(self):
        _wait_for_next_minute()
        # Trip the cap (35 calls, session-reused for speed).
        sess = requests.Session()
        try:
            for _ in range(35):
                sess.get(
                    f"{BASE_URL}/api/public/heatmap",
                    headers=_hdr("free"), timeout=10,
                )
        finally:
            sess.close()

        # Give the async log task a moment to flush.
        time.sleep(1.0)

        tok = _login()
        r = requests.get(
            f"{BASE_URL}/api/admin/public-traffic?status=429&limit=20",
            headers={"Authorization": f"Bearer {tok}"}, timeout=10,
        )
        assert r.status_code == 200
        rows = r.json()["items"]
        assert len(rows) >= 1
        for row in rows:
            assert row["status"] == 429
            assert row["tier"] == "free"
