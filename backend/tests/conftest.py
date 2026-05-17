import os
import pytest
import pytest_asyncio  # noqa: F401 — ensures the plugin is loaded
import requests

# Seed DB connection env vars BEFORE any test module imports `db` /
# `namespaces` / `shared.*` (those read env at import time and KeyError
# if MONGO_URL isn't set). The backend .env is the source of truth.
if not os.environ.get("MONGO_URL"):
    be_env = "/app/backend/.env"
    if os.path.exists(be_env):
        with open(be_env) as f:
            for line in f:
                line = line.strip()
                if line.startswith("MONGO_URL=") and "MONGO_URL" not in os.environ:
                    os.environ["MONGO_URL"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("DB_NAME=") and "DB_NAME" not in os.environ:
                    os.environ["DB_NAME"] = line.split("=", 1)[1].strip().strip('"').strip("'")
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
