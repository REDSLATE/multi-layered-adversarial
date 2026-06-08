import os
import pytest
import pytest_asyncio  # noqa: F401 — ensures the plugin is loaded
import requests

# Seed env vars BEFORE any test module imports `shared.*` modules
# that read env at import time. The backend .env is the source of
# truth for everything; tests share that view so any env-tuned
# value (caps, ladder, neutral-brain flags) matches the live
# backend the tests are HTTP-poking.
#
# 2026-06-07: expanded from MONGO_URL/DB_NAME only to "all keys".
# Triggered by the $500 live-pilot cap tightening — without
# /app/backend/.env loaded fully, exposure_caps.py defaulted to the
# pre-pilot $100k constants in the test process while the backend
# saw the env-overridden $25, breaking the parity assertion.
_be_env = "/app/backend/.env"
if os.path.exists(_be_env):
    with open(_be_env) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            _k = _k.strip()
            if _k and _k not in os.environ:
                os.environ[_k] = _v.strip().strip('"').strip("'")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_database")

# ───────── session-scoped event loop ─────────────────────────────────
# Motor's AsyncIOMotorClient binds to the first event loop that uses
# it. The plugin defaults to a fresh loop per test, which makes Motor
# throw `RuntimeError: Event loop is closed` on the 2nd Mongo test.
# Session scope (configured in pytest.ini via
# asyncio_default_test_loop_scope = session) keeps one loop alive for
# the whole suite so Motor stays happy.

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    # Fallback: read from frontend/.env
    env_path = "/app/frontend/.env"
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip()
                    break
BASE_URL = (BASE_URL or "").rstrip("/")

ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL", "admin@risedual.io")
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD", "risedual-admin-2026")


@pytest.fixture(scope="session")
def base_url():
    assert BASE_URL, "REACT_APP_BACKEND_URL is required"
    return BASE_URL


@pytest.fixture()
def api_client():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def admin_token():
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=30,
    )
    if r.status_code != 200:
        pytest.skip(f"Login failed: {r.status_code} {r.text}")
    return r.json()["access_token"]


@pytest.fixture()
def auth_client(api_client, admin_token):
    api_client.headers.update({"Authorization": f"Bearer {admin_token}"})
    return api_client
