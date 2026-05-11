"""IBKR connection backend tests.

Mirrors the Kraken test pattern. We can't hit the real IBKR Web API
from CI, so we verify the parts that don't need a live broker:
  - /admin/ibkr/status returns the disconnected shape with no creds.
  - /admin/ibkr/connect rejects malformed inputs at the schema layer.
  - /admin/ibkr/{test, tickle, accounts, positions} return 404 when
    no creds are stored.
  - /admin/ibkr/disconnect is idempotent.
  - /admin/ibkr/execution rejects without the confirmation phrase, and
    accepts the literal phrase once a credential doc exists.
  - /admin/ibkr/audit captures the disconnect action.
  - Every admin endpoint requires JWT auth.
  - shared.ibkr.get_active() returns None when nothing is stored.
"""
import os

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

ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PASSWORD = "risedual-admin-2026"


def _login() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=20,
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _hdr(t: str) -> dict:
    return {"Authorization": f"Bearer {t}", "Content-Type": "application/json"}


def _disconnect(tok: str) -> None:
    requests.delete(
        f"{BASE_URL}/api/admin/ibkr/disconnect", headers=_hdr(tok), timeout=10,
    )


# ───────────────── unit ─────────────────

class TestActiveResolver:
    def test_get_active_none_when_missing(self):
        """shared.ibkr.get_active returns None when nothing is stored."""
        import asyncio
        from pathlib import Path

        env_path = Path("/app/backend/.env")
        env: dict[str, str] = {}
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
        os.environ.setdefault("MONGO_URL", env["MONGO_URL"])
        os.environ.setdefault("DB_NAME", env["DB_NAME"])

        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        seed_db = client[os.environ["DB_NAME"]]

        async def _check():
            await seed_db["ibkr_credentials"].delete_one({"_id": "singleton"})
            from shared.ibkr import get_active
            assert await get_active() is None

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_check())
        finally:
            client.close()
            loop.close()


# ───────────────── API surface ─────────────────

class TestIBKRRoutes:
    def test_status_no_creds(self):
        tok = _login()
        _disconnect(tok)
        r = requests.get(
            f"{BASE_URL}/api/admin/ibkr/status", headers=_hdr(tok), timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        assert d["connected"] is False
        assert d["execution_enabled"] is False
        assert d["tickler_running"] is False

    def test_connect_rejects_short_token_422(self):
        tok = _login()
        r = requests.post(
            f"{BASE_URL}/api/admin/ibkr/connect",
            headers=_hdr(tok),
            json={"access_token": "tooshort"},
            timeout=10,
        )
        assert r.status_code == 422

    def test_connect_rejects_missing_token_422(self):
        tok = _login()
        r = requests.post(
            f"{BASE_URL}/api/admin/ibkr/connect",
            headers=_hdr(tok),
            json={"account_id": "DU123456"},
            timeout=10,
        )
        assert r.status_code == 422

    def test_connect_rejects_non_https_base_url_422(self):
        tok = _login()
        r = requests.post(
            f"{BASE_URL}/api/admin/ibkr/connect",
            headers=_hdr(tok),
            json={
                "access_token": "x" * 40,
                "base_url": "http://insecure.example.com",
            },
            timeout=10,
        )
        assert r.status_code == 422

    def test_test_404_when_unconfigured(self):
        tok = _login()
        _disconnect(tok)
        r = requests.post(
            f"{BASE_URL}/api/admin/ibkr/test", headers=_hdr(tok), timeout=10,
        )
        assert r.status_code == 404

    def test_tickle_404_when_unconfigured(self):
        tok = _login()
        _disconnect(tok)
        r = requests.post(
            f"{BASE_URL}/api/admin/ibkr/tickle", headers=_hdr(tok), timeout=10,
        )
        assert r.status_code == 404

    def test_accounts_404_when_unconfigured(self):
        tok = _login()
        _disconnect(tok)
        r = requests.get(
            f"{BASE_URL}/api/admin/ibkr/accounts", headers=_hdr(tok), timeout=10,
        )
        assert r.status_code == 404

    def test_positions_404_when_no_account(self):
        tok = _login()
        _disconnect(tok)
        r = requests.get(
            f"{BASE_URL}/api/admin/ibkr/positions", headers=_hdr(tok), timeout=10,
        )
        assert r.status_code == 404

    def test_disconnect_idempotent(self):
        tok = _login()
        r = requests.delete(
            f"{BASE_URL}/api/admin/ibkr/disconnect", headers=_hdr(tok), timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_execution_toggle_404_when_unconfigured(self):
        tok = _login()
        _disconnect(tok)
        r = requests.post(
            f"{BASE_URL}/api/admin/ibkr/execution",
            headers=_hdr(tok),
            json={"enabled": True, "confirm": "I authorize execution on IBKR"},
            timeout=10,
        )
        assert r.status_code == 404

    def test_execution_toggle_requires_confirm_phrase(self):
        """Inject a fake credential doc directly, then exercise the
        confirmation-phrase guard without any real IBKR token."""
        import asyncio
        from pathlib import Path

        env_path = Path("/app/backend/.env")
        env: dict[str, str] = {}
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
        os.environ.setdefault("MONGO_URL", env["MONGO_URL"])
        os.environ.setdefault("DB_NAME", env["DB_NAME"])

        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        seed_db = client[os.environ["DB_NAME"]]

        async def _seed():
            await seed_db["ibkr_credentials"].replace_one(
                {"_id": "singleton"},
                {
                    "_id": "singleton",
                    "base_url": "https://api.ibkr.com",
                    "encrypted_access_token": "",  # toggle-only doesn't need
                    "token_preview": "test***",
                    "account_id": None,
                    "accounts": [],
                    "auth_status": {"authenticated": False},
                    "execution_enabled": False,
                    "created_at": "2026-02-09T00:00:00+00:00",
                    "updated_at": "2026-02-09T00:00:00+00:00",
                    "connected_by": "test",
                },
                upsert=True,
            )

        async def _cleanup():
            await seed_db["ibkr_credentials"].delete_one({"_id": "singleton"})
            await seed_db["ibkr_audit_log"].delete_many({"actor": "admin@risedual.io"})

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_seed())
            tok = _login()
            # Wrong phrase
            r = requests.post(
                f"{BASE_URL}/api/admin/ibkr/execution",
                headers=_hdr(tok),
                json={"enabled": True, "confirm": "yes"},
                timeout=10,
            )
            assert r.status_code == 400
            assert "phrase" in r.text.lower()
            # Right phrase — enable
            r = requests.post(
                f"{BASE_URL}/api/admin/ibkr/execution",
                headers=_hdr(tok),
                json={"enabled": True, "confirm": "I authorize execution on IBKR"},
                timeout=10,
            )
            assert r.status_code == 200
            assert r.json()["execution_enabled"] is True
            # Right phrase — disable
            r = requests.post(
                f"{BASE_URL}/api/admin/ibkr/execution",
                headers=_hdr(tok),
                json={"enabled": False, "confirm": "Disable execution"},
                timeout=10,
            )
            assert r.status_code == 200
            assert r.json()["execution_enabled"] is False
        finally:
            loop.run_until_complete(_cleanup())
            client.close()
            loop.close()

    def test_audit_log_records_disconnect(self):
        tok = _login()
        _disconnect(tok)
        r = requests.get(
            f"{BASE_URL}/api/admin/ibkr/audit", headers=_hdr(tok), timeout=10,
        )
        assert r.status_code == 200
        items = r.json()["items"]
        assert any(it["action"] == "ibkr_disconnect" for it in items[:10])

    def test_auth_required(self):
        # All admin endpoints behind JWT.
        for verb, path, body in [
            ("get",  "/api/admin/ibkr/status",       None),
            ("post", "/api/admin/ibkr/connect",      {"access_token": "x" * 40}),
            ("post", "/api/admin/ibkr/test",         {}),
            ("post", "/api/admin/ibkr/tickle",       {}),
            ("get",  "/api/admin/ibkr/accounts",     None),
            ("get",  "/api/admin/ibkr/positions",    None),
            ("delete","/api/admin/ibkr/disconnect",  None),
            ("post", "/api/admin/ibkr/execution",
                {"enabled": False, "confirm": "Disable execution"}),
            ("get",  "/api/admin/ibkr/audit",        None),
        ]:
            fn = getattr(requests, verb)
            kwargs = {"timeout": 10}
            if body is not None:
                kwargs["json"] = body
            r = fn(f"{BASE_URL}{path}", **kwargs)
            assert r.status_code in (401, 403), f"{verb.upper()} {path} → {r.status_code}"
