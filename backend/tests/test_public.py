"""Public.com connection backend tests.

Mirrors the IBKR test pattern. We can't hit the real Public.com API
from CI, so we verify the parts that don't need a live broker:
  - /admin/public/status returns the disconnected shape with no creds.
  - /admin/public/connect rejects malformed inputs at the schema layer.
  - /admin/public/{test, refresh-token, accounts, portfolio} return 404
    when no creds are stored.
  - /admin/public/disconnect is idempotent.
  - /admin/public/execution rejects without the confirmation phrase, and
    accepts the literal phrase once a credential doc exists.
  - /admin/public/audit captures the disconnect action.
  - Every admin endpoint requires JWT auth.
  - shared.public.get_active() returns None when nothing is stored.
"""
import os

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
        f"{BASE_URL}/api/admin/public/disconnect", headers=_hdr(tok), timeout=10,
    )


# Note: the `get_active() returns None` unit test exists in test_ibkr.py;
# we don't duplicate the global-db-event-loop pattern here because pytest
# session reuse causes "Event loop is closed" when both modules redo it.
# The HTTP-level `test_status_no_creds` below covers the same code path.


# ───────────────── API surface ─────────────────

class TestPublicRoutes:
    def test_status_no_creds(self):
        tok = _login()
        _disconnect(tok)
        r = requests.get(
            f"{BASE_URL}/api/admin/public/status", headers=_hdr(tok), timeout=10,
        )
        assert r.status_code == 200
        d = r.json()
        assert d["connected"] is False
        assert d["execution_enabled"] is False
        assert d["refresher_running"] is False

    def test_connect_rejects_short_secret_422(self):
        tok = _login()
        r = requests.post(
            f"{BASE_URL}/api/admin/public/connect",
            headers=_hdr(tok),
            json={"secret": "tooshort"},
            timeout=10,
        )
        assert r.status_code == 422

    def test_connect_rejects_missing_secret_422(self):
        tok = _login()
        r = requests.post(
            f"{BASE_URL}/api/admin/public/connect",
            headers=_hdr(tok),
            json={"account_id": "ABC123"},
            timeout=10,
        )
        assert r.status_code == 422

    def test_connect_rejects_non_https_base_url_422(self):
        tok = _login()
        r = requests.post(
            f"{BASE_URL}/api/admin/public/connect",
            headers=_hdr(tok),
            json={
                "secret": "x" * 40,
                "base_url": "http://insecure.example.com",
            },
            timeout=10,
        )
        assert r.status_code == 422

    def test_connect_rejects_zero_validity_422(self):
        tok = _login()
        r = requests.post(
            f"{BASE_URL}/api/admin/public/connect",
            headers=_hdr(tok),
            json={
                "secret": "x" * 40,
                "token_validity_minutes": 0,
            },
            timeout=10,
        )
        assert r.status_code == 422

    def test_connect_rejects_excessive_validity_422(self):
        tok = _login()
        r = requests.post(
            f"{BASE_URL}/api/admin/public/connect",
            headers=_hdr(tok),
            json={
                "secret": "x" * 40,
                "token_validity_minutes": 999999,  # > 7d cap
            },
            timeout=10,
        )
        assert r.status_code == 422

    def test_test_404_when_unconfigured(self):
        tok = _login()
        _disconnect(tok)
        r = requests.post(
            f"{BASE_URL}/api/admin/public/test", headers=_hdr(tok), timeout=10,
        )
        assert r.status_code == 404

    def test_refresh_token_404_when_unconfigured(self):
        tok = _login()
        _disconnect(tok)
        r = requests.post(
            f"{BASE_URL}/api/admin/public/refresh-token",
            headers=_hdr(tok),
            timeout=10,
        )
        assert r.status_code == 404

    def test_accounts_404_when_unconfigured(self):
        tok = _login()
        _disconnect(tok)
        r = requests.get(
            f"{BASE_URL}/api/admin/public/accounts", headers=_hdr(tok), timeout=10,
        )
        assert r.status_code == 404

    def test_portfolio_404_when_no_account(self):
        tok = _login()
        _disconnect(tok)
        r = requests.get(
            f"{BASE_URL}/api/admin/public/portfolio", headers=_hdr(tok), timeout=10,
        )
        assert r.status_code == 404

    def test_disconnect_idempotent(self):
        tok = _login()
        r = requests.delete(
            f"{BASE_URL}/api/admin/public/disconnect", headers=_hdr(tok), timeout=10,
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_execution_toggle_404_when_unconfigured(self):
        tok = _login()
        _disconnect(tok)
        r = requests.post(
            f"{BASE_URL}/api/admin/public/execution",
            headers=_hdr(tok),
            json={"enabled": True, "confirm": "I authorize execution on Public"},
            timeout=10,
        )
        assert r.status_code == 404

    def test_execution_toggle_requires_confirm_phrase(self):
        """Inject a fake credential doc directly, then exercise the
        confirmation-phrase guard without any real Public secret."""
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
            await seed_db["public_credentials"].replace_one(
                {"_id": "singleton"},
                {
                    "_id": "singleton",
                    "base_url": "https://api.public.com",
                    "encrypted_secret": "",
                    "secret_preview": "test***",
                    "encrypted_access_token": "",
                    "access_token_expires_at": None,
                    "token_validity_minutes": 1440,
                    "account_id": None,
                    "accounts": [],
                    "execution_enabled": False,
                    "created_at": "2026-02-11T00:00:00+00:00",
                    "updated_at": "2026-02-11T00:00:00+00:00",
                    "connected_by": "test",
                },
                upsert=True,
            )

        async def _cleanup():
            await seed_db["public_credentials"].delete_one({"_id": "singleton"})
            await seed_db["public_audit_log"].delete_many({"actor": "admin@risedual.io"})

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_seed())
            tok = _login()
            # Wrong phrase
            r = requests.post(
                f"{BASE_URL}/api/admin/public/execution",
                headers=_hdr(tok),
                json={"enabled": True, "confirm": "yes"},
                timeout=10,
            )
            assert r.status_code == 400
            assert "phrase" in r.text.lower()
            # Right phrase — enable
            r = requests.post(
                f"{BASE_URL}/api/admin/public/execution",
                headers=_hdr(tok),
                json={"enabled": True, "confirm": "I authorize execution on Public"},
                timeout=10,
            )
            assert r.status_code == 200
            assert r.json()["execution_enabled"] is True
            # Right phrase — disable
            r = requests.post(
                f"{BASE_URL}/api/admin/public/execution",
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
            f"{BASE_URL}/api/admin/public/audit", headers=_hdr(tok), timeout=10,
        )
        assert r.status_code == 200
        items = r.json()["items"]
        assert any(it["action"] == "public_disconnect" for it in items[:10])

    def test_auth_required(self):
        # All admin endpoints behind JWT.
        for verb, path, body in [
            ("get",   "/api/admin/public/status",        None),
            ("post",  "/api/admin/public/connect",       {"secret": "x" * 40}),
            ("post",  "/api/admin/public/test",          {}),
            ("post",  "/api/admin/public/refresh-token", {}),
            ("get",   "/api/admin/public/accounts",      None),
            ("get",   "/api/admin/public/portfolio",     None),
            ("delete","/api/admin/public/disconnect",    None),
            ("post",  "/api/admin/public/execution",
                {"enabled": False, "confirm": "Disable execution"}),
            ("get",   "/api/admin/public/audit",         None),
        ]:
            fn = getattr(requests, verb)
            kwargs = {"timeout": 10}
            if body is not None:
                kwargs["json"] = body
            r = fn(f"{BASE_URL}{path}", **kwargs)
            assert r.status_code in (401, 403), f"{verb.upper()} {path} → {r.status_code}"
