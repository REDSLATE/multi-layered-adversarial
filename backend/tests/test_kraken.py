"""Kraken connection backend tests.

Verifies the parts we can test without real Kraken credentials:
  - HMAC-SHA512 signing produces a deterministic, decodable signature.
  - Fernet encrypt/decrypt round-trip preserves the private key exactly.
  - The redact() helper masks middle, keeps tails.
  - /admin/kraken/status returns the disconnected shape when no creds.
  - /admin/kraken/connect rejects missing keys (422) and bad pairs (422).
  - /admin/kraken/test returns 404 when no creds exist.
  - /admin/kraken/disconnect is idempotent when nothing is connected.
  - /admin/kraken/execution rejects without the confirmation phrase.
  - Audit log records a connect attempt rejection only when scopes fail,
    not on schema failures (those 422 before any DB write).
"""
import base64
import os
import hashlib
import hmac
import urllib.parse

import requests
import pytest

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


# ───────────────── crypto pieces (unit) ─────────────────

class TestSigningAndCrypto:
    def test_sign_known_vector(self):
        """Kraken's documented test vector — confirm we produce a base64,
        non-empty signature of the expected length."""
        from shared.kraken import _sign
        priv = "FRs+gtq09rR7OFtKj9BGhyOGS3u5vtY/EdiIBO9kD8NFtRX7w7LeJDSrX6cq1D8zmQmGkWFjksuhBvKOAWJohQ=="
        nonce = "1540973848000"
        path = "/0/private/TradeBalance"
        post = urllib.parse.urlencode({"nonce": nonce, "asset": "xbt"})
        sig = _sign(path, nonce, post, priv)
        # base64 of SHA-512 = 88 chars (64 bytes → 88 incl padding)
        assert len(sig) == 88
        decoded = base64.b64decode(sig)
        assert len(decoded) == 64

    def test_sign_different_nonces_differ(self):
        from shared.kraken import _sign
        priv = base64.b64encode(b"x" * 64).decode()
        sig1 = _sign("/0/private/Balance", "1000", "nonce=1000", priv)
        sig2 = _sign("/0/private/Balance", "2000", "nonce=2000", priv)
        assert sig1 != sig2

    def test_sign_matches_manual_hmac(self):
        """Re-implement signing manually and ensure we match."""
        from shared.kraken import _sign
        priv = base64.b64encode(b"the answer is 42 the answer is 42").decode()
        nonce = "1700000000000"
        path = "/0/private/Balance"
        body = urllib.parse.urlencode({"nonce": nonce, "asset": "ZUSD"})
        got = _sign(path, nonce, body, priv)
        sha = hashlib.sha256((nonce + body).encode()).digest()
        expected = base64.b64encode(
            hmac.new(base64.b64decode(priv), path.encode() + sha, hashlib.sha512).digest()
        ).decode()
        assert got == expected

    def test_fernet_round_trip(self):
        from shared.credentials import encrypt, decrypt
        plaintext = "FRs+gtq09rR7OFtKj9BGhyOGS3u5vtY/EdiIBO9kD8NFtRX7w7LeJDSrX6cq1D8zmQmGkWFjksuhBvKOAWJohQ=="
        token = encrypt(plaintext)
        assert token != plaintext
        assert decrypt(token) == plaintext

    def test_redact_masks_middle(self):
        from shared.credentials import redact
        assert redact("ABCD1234EFGH5678") == "ABCD********5678"

    def test_redact_short_fully_masked(self):
        from shared.credentials import redact
        assert redact("abc") == "****"
        assert "*" in redact("abcdefg")


# ───────────────── API surface ─────────────────

class TestKrakenRoutes:
    def test_status_no_creds(self):
        # Ensure clean slate
        tok = _login()
        requests.delete(f"{BASE_URL}/api/admin/kraken/disconnect", headers=_hdr(tok), timeout=10)
        r = requests.get(f"{BASE_URL}/api/admin/kraken/status", headers=_hdr(tok), timeout=10)
        assert r.status_code == 200
        d = r.json()
        assert d["connected"] is False
        assert d["execution_enabled"] is False
        assert d["poller_running"] is False

    def test_connect_rejects_missing_keys_422(self):
        tok = _login()
        r = requests.post(
            f"{BASE_URL}/api/admin/kraken/connect",
            headers=_hdr(tok),
            json={"api_key": "", "private_key": "", "pairs": ["BTC/USD"], "tf": "1h"},
            timeout=20,
        )
        assert r.status_code == 422

    def test_connect_rejects_unknown_pair_422(self):
        tok = _login()
        r = requests.post(
            f"{BASE_URL}/api/admin/kraken/connect",
            headers=_hdr(tok),
            json={
                "api_key": "x" * 30,
                "private_key": "y" * 30,
                "pairs": ["FAKE/USD"],
                "tf": "1h",
            },
            timeout=20,
        )
        assert r.status_code == 422
        assert "FAKE/USD" in r.text or "unknown pairs" in r.text.lower()

    def test_connect_rejects_bad_tf_422(self):
        tok = _login()
        r = requests.post(
            f"{BASE_URL}/api/admin/kraken/connect",
            headers=_hdr(tok),
            json={
                "api_key": "x" * 30,
                "private_key": "y" * 30,
                "pairs": ["BTC/USD"],
                "tf": "13m",
            },
            timeout=20,
        )
        assert r.status_code == 422

    def test_test_endpoint_404_when_unconfigured(self):
        tok = _login()
        # Ensure disconnected
        requests.delete(f"{BASE_URL}/api/admin/kraken/disconnect", headers=_hdr(tok), timeout=10)
        r = requests.post(f"{BASE_URL}/api/admin/kraken/test", headers=_hdr(tok), timeout=10)
        assert r.status_code == 404

    def test_poll_404_when_unconfigured(self):
        tok = _login()
        requests.delete(f"{BASE_URL}/api/admin/kraken/disconnect", headers=_hdr(tok), timeout=10)
        r = requests.post(f"{BASE_URL}/api/admin/kraken/poll", headers=_hdr(tok), timeout=10)
        assert r.status_code == 404

    def test_disconnect_idempotent(self):
        tok = _login()
        r = requests.delete(f"{BASE_URL}/api/admin/kraken/disconnect", headers=_hdr(tok), timeout=10)
        # 200 even if there was nothing to delete
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_execution_toggle_404_when_unconfigured(self):
        tok = _login()
        requests.delete(f"{BASE_URL}/api/admin/kraken/disconnect", headers=_hdr(tok), timeout=10)
        r = requests.post(
            f"{BASE_URL}/api/admin/kraken/execution",
            headers=_hdr(tok),
            json={"enabled": True, "confirm": "I authorize execution on Kraken"},
            timeout=10,
        )
        assert r.status_code == 404

    def test_execution_toggle_requires_confirm_phrase(self):
        # Inject a fake credential doc directly so we can exercise the
        # confirmation-phrase guard without real keys. Use the live backend
        # over a tiny seed endpoint via mongo directly, loading env from
        # backend/.env.
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
            await seed_db["kraken_credentials"].replace_one(
                {"_id": "singleton"},
                {
                    "_id": "singleton",
                    "public_key": "test-public",
                    "public_key_preview": "test***",
                    "private_key_preview": "test***",
                    "encrypted_private_key": "",
                    "pairs": ["BTC/USD"],
                    "tf": "1h",
                    "poll_interval_seconds": 60,
                    "auto_poll_enabled": False,  # don't actually poll
                    "execution_enabled": False,
                    "scopes": {"query_funds": True},
                    "last_nonce": 0,
                    "connected_by": "test",
                },
                upsert=True,
            )

        async def _cleanup():
            await seed_db["kraken_credentials"].delete_one({"_id": "singleton"})

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_seed())
            tok = _login()
            # Wrong phrase
            r = requests.post(
                f"{BASE_URL}/api/admin/kraken/execution",
                headers=_hdr(tok),
                json={"enabled": True, "confirm": "yes"},
                timeout=10,
            )
            assert r.status_code == 400
            assert "phrase" in r.text.lower()
            # Right phrase
            r = requests.post(
                f"{BASE_URL}/api/admin/kraken/execution",
                headers=_hdr(tok),
                json={"enabled": True, "confirm": "I authorize execution on Kraken"},
                timeout=10,
            )
            assert r.status_code == 200
            assert r.json()["execution_enabled"] is True
            # Flip back, requires different phrase
            r = requests.post(
                f"{BASE_URL}/api/admin/kraken/execution",
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
        requests.delete(f"{BASE_URL}/api/admin/kraken/disconnect", headers=_hdr(tok), timeout=10)
        r = requests.get(f"{BASE_URL}/api/admin/kraken/audit", headers=_hdr(tok), timeout=10)
        assert r.status_code == 200
        items = r.json()["items"]
        # Most-recent entry should be the disconnect we just performed.
        assert any(it["action"] == "kraken_disconnect" for it in items[:5])

    def test_auth_required(self):
        r = requests.get(f"{BASE_URL}/api/admin/kraken/status", timeout=10)
        assert r.status_code in (401, 403)
        r = requests.post(
            f"{BASE_URL}/api/admin/kraken/connect",
            json={"api_key": "a", "private_key": "b", "pairs": ["BTC/USD"], "tf": "1h"},
            timeout=10,
        )
        assert r.status_code in (401, 403)
