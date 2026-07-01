"""Tests for /app/trader/webull_auth.py — 2FA token lifecycle."""
from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx
import pytest

sys.path.insert(0, "/app")

from trader import config, store, spread, webull_auth  # noqa: E402


@pytest.fixture()
def fresh_env(tmp_path, monkeypatch):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    store.init(
        str(tmp_path / "executions.sqlite"),
        str(tmp_path / "jsonl"),
    )
    # Point the token file at the tmp dir
    monkeypatch.setenv("WEBULL_TOKEN_PATH", str(tmp_path / "webull_token.json"))
    # Force env-based fallback to be empty so tests hit the disk cache
    monkeypatch.delenv("WEBULL_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("WEBULL_APP_KEY", "test-key")
    monkeypatch.setenv("WEBULL_APP_SECRET", "test-secret")
    # Reset the module-level cache between tests
    webull_auth._cache = None
    spread._latest.clear()
    yield tmp_path
    webull_auth._cache = None
    store.close()
    loop.close()
    asyncio.set_event_loop(None)


def test_get_token_returns_none_when_missing(fresh_env):
    assert webull_auth.get_token() is None
    s = webull_auth.status()
    assert s["present"] is False


@pytest.mark.asyncio
async def test_create_token_persists_and_sanitizes(fresh_env, monkeypatch):
    """Happy path: Webull returns a PENDING token, we persist it and
    return a sanitized preview (no raw token over the wire)."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["headers"] = dict(request.headers)
        seen["method"] = request.method
        return httpx.Response(200, json={
            "token": "ccb071f764864b65a1fb48484e940a56",
            "expires": int(time.time() * 1000) + 15 * 24 * 3600 * 1000,
            "status": "PENDING",
        })

    # Patch httpx.AsyncClient so create_token() hits our mock
    orig = httpx.AsyncClient

    def _mock_client(**kw):
        kw.pop("timeout", None)
        return orig(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(webull_auth.httpx, "AsyncClient", _mock_client)

    result = await webull_auth.create_token()

    assert result["status"] == "PENDING"
    assert "token" not in result, "raw token must not be surfaced"
    assert result["token_preview"].startswith("ccb071")
    assert result["token_length"] == 32
    # Verify signed POST with empty body to the right path
    assert seen["path"] == "/openapi/auth/token/create"
    assert seen["method"] == "POST"
    for h in [
        "x-app-key", "x-timestamp", "x-signature-nonce",
        "x-signature-algorithm", "x-signature-version",
        "x-signature", "x-version",
    ]:
        assert seen["headers"].get(h), f"missing required header: {h}"
    # token/create must NOT have x-access-token or x-app-secret
    assert "x-access-token" not in seen["headers"]
    assert "x-app-secret" not in seen["headers"]
    # Disk persistence
    disk = json.loads((fresh_env / "webull_token.json").read_text())
    assert disk["token"] == "ccb071f764864b65a1fb48484e940a56"
    assert disk["status"] == "PENDING"


@pytest.mark.asyncio
async def test_create_token_raises_on_401(fresh_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={
            "error_code": "UNAUTHORIZED",
            "message": "Insufficient permission",
        })

    orig = httpx.AsyncClient

    def _mock_client(**kw):
        kw.pop("timeout", None)
        return orig(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(webull_auth.httpx, "AsyncClient", _mock_client)
    with pytest.raises(RuntimeError, match="401"):
        await webull_auth.create_token()


def test_get_token_returns_none_when_expired(fresh_env):
    # Seed a token that expired yesterday
    (fresh_env / "webull_token.json").write_text(json.dumps({
        "token": "abc123def456",
        "expires": int(time.time() * 1000) - 60_000,
        "status": "NORMAL",
    }))
    assert webull_auth.get_token() is None
    s = webull_auth.status()
    assert s["expired"] is True


def test_get_token_returns_value_when_valid(fresh_env):
    tok = "ccb071f764864b65a1fb48484e940a56"
    (fresh_env / "webull_token.json").write_text(json.dumps({
        "token": tok,
        "expires": int(time.time() * 1000) + 24 * 3600 * 1000,
        "status": "NORMAL",
    }))
    assert webull_auth.get_token() == tok


def test_spread_creds_prefers_persisted_token(fresh_env):
    """When webull_auth has a token, spread._webull_creds() must
    return it, not fall back to the env var."""
    tok = "ffffffffffffffffffffffffffffffff"
    (fresh_env / "webull_token.json").write_text(json.dumps({
        "token": tok,
        "expires": int(time.time() * 1000) + 24 * 3600 * 1000,
        "status": "NORMAL",
    }))
    # Force clear the module cache since fixture set it after fresh_env
    webull_auth._cache = None
    creds = spread._webull_creds()
    assert creds is not None
    assert creds[2] == tok


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
