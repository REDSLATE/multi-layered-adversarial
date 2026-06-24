"""Regression tests for the 2026-06-24 401-cascade hotfix.

The bug (operator-reported 5:10 PM screenshot, prod): after the 60-min
access token expired mid-session, EVERY admin panel rendered inline
"HTTP 401" while the sidebar still showed the operator signed in.
The frontend never called /api/auth/refresh, so the session silently
died.

The fix has two halves:
  1. Backend: `/api/auth/refresh` now returns `{access_token: ...}` in
     the response body (in addition to setting the cookie). The
     frontend uses localStorage for its bearer token, so the cookie
     alone is not sufficient.
  2. Frontend: `api.js` request() now has a 401 auto-refresh
     interceptor (tested separately on the JS side).

This file covers the backend half.

Doctrine pins:
  * /refresh response body MUST contain `access_token`.
  * /refresh still works as a no-op for an already-valid session (the
    refresh-from-cookie path).
  * /refresh with NO refresh cookie returns 401 (don't issue tokens
    to anonymous callers).
  * /refresh with an expired refresh cookie returns 401.
  * /refresh with an ACCESS token in the refresh-cookie slot is
    rejected (must be type=refresh).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

import jwt
import pytest

pytestmark = pytest.mark.asyncio


JWT_ALGORITHM = "HS256"


def _secret() -> str:
    return os.environ["JWT_SECRET"]


@pytest.fixture
async def admin_user():
    """Ensure the admin user exists and yield their id."""
    from auth import seed_admin
    from db import db
    await seed_admin(db)
    user = await db.users.find_one(
        {"email": os.environ.get("ADMIN_EMAIL", "admin@risedual.io").lower()},
        {"_id": 0, "id": 1, "email": 1},
    )
    assert user is not None
    return user


# ── /refresh returns the new access token in the body ──────────────
async def test_refresh_returns_access_token_in_body(admin_user):
    """The body MUST contain `access_token` so the localStorage-based
    frontend can persist it. Cookie alone leaves the bearer header
    untouched and re-triggers the 401 cascade."""
    from httpx import AsyncClient, ASGITransport
    from server import app

    # Mint a valid refresh token for the admin user (no need to drive
    # a full login round-trip — we're testing the /refresh endpoint
    # in isolation here).
    refresh_jwt = jwt.encode(
        {
            "sub": admin_user["id"],
            "exp": datetime.now(timezone.utc) + timedelta(days=7),
            "type": "refresh",
        },
        _secret(),
        algorithm=JWT_ALGORITHM,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/auth/refresh",
            cookies={"refresh_token": refresh_jwt},
        )
    assert r.status_code == 200, f"refresh failed: {r.status_code} {r.text}"
    payload = r.json()
    assert payload.get("ok") is True
    assert "access_token" in payload, (
        f"refresh body missing access_token: {payload}. "
        "Frontend can't reauth without it."
    )
    assert payload.get("token_type") == "bearer"

    # Returned token MUST be a valid access JWT.
    decoded = jwt.decode(payload["access_token"], _secret(), algorithms=[JWT_ALGORITHM])
    assert decoded.get("type") == "access"
    assert decoded.get("sub") == admin_user["id"]


# ── /refresh rejects missing cookie ────────────────────────────────
async def test_refresh_without_cookie_is_401():
    """Anonymous /refresh callers must NOT get a token."""
    from httpx import AsyncClient, ASGITransport
    from server import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/auth/refresh")
    assert r.status_code == 401


# ── /refresh rejects an ACCESS token in the refresh slot ───────────
async def test_refresh_rejects_access_token_in_cookie_slot(admin_user):
    """Type confusion guard — the cookie MUST be type=refresh.
    If the operator's frontend ever mis-routes the access token into
    the refresh cookie slot, the server must NOT honor it."""
    from httpx import AsyncClient, ASGITransport
    from server import app

    access_jwt = jwt.encode(
        {
            "sub": admin_user["id"],
            "email": admin_user["email"],
            "exp": datetime.now(timezone.utc) + timedelta(minutes=60),
            "type": "access",   # ← intentionally wrong type for /refresh
        },
        _secret(),
        algorithm=JWT_ALGORITHM,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/auth/refresh",
            cookies={"refresh_token": access_jwt},
        )
    assert r.status_code == 401, (
        f"server accepted an access-token in the refresh slot: "
        f"{r.status_code} {r.text}"
    )


# ── /refresh rejects an expired refresh token ──────────────────────
async def test_refresh_rejects_expired_token(admin_user):
    """Expired refresh tokens MUST NOT mint new access tokens."""
    from httpx import AsyncClient, ASGITransport
    from server import app

    expired_refresh = jwt.encode(
        {
            "sub": admin_user["id"],
            "exp": datetime.now(timezone.utc) - timedelta(days=1),  # expired
            "type": "refresh",
        },
        _secret(),
        algorithm=JWT_ALGORITHM,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/auth/refresh",
            cookies={"refresh_token": expired_refresh},
        )
    assert r.status_code == 401


# ── End-to-end: login → wait → refresh → reuse on admin endpoint ───
async def test_login_then_refresh_then_admin_endpoint(admin_user):
    """Full round-trip: log in (real flow), POST /refresh, use the
    NEW access token from the response body to call an admin endpoint."""
    from httpx import AsyncClient, ASGITransport
    from server import app
    from db import db

    # Cleanup any leftover login_attempts rows from previous tests.
    await db.login_attempts.drop()

    admin_email = os.environ.get("ADMIN_EMAIL", "admin@risedual.io").lower()
    admin_password = os.environ.get("ADMIN_PASSWORD", "risedual-admin-2026")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login_resp = await client.post(
            "/api/auth/login",
            json={"email": admin_email, "password": admin_password},
        )
        assert login_resp.status_code == 200, login_resp.text
        # In production the refresh_token cookie is set with
        # `secure=True, samesite=none`, which the httpx test client
        # over plain http:// can refuse to send back. Pull the value
        # straight from the Set-Cookie header and pass it manually so
        # the test isn't fighting the cookie jar's secure-flag policy.
        refresh_cookie = login_resp.cookies.get("refresh_token")
        if refresh_cookie is None:
            # Fallback: parse from Set-Cookie header directly.
            set_cookie = login_resp.headers.get("set-cookie", "")
            for chunk in set_cookie.split(","):
                if "refresh_token=" in chunk:
                    refresh_cookie = chunk.split("refresh_token=")[1].split(";")[0]
                    break
        assert refresh_cookie, (
            f"login did not set a refresh_token cookie. "
            f"headers={dict(login_resp.headers)}"
        )

        refresh_resp = await client.post(
            "/api/auth/refresh",
            cookies={"refresh_token": refresh_cookie},
        )
        assert refresh_resp.status_code == 200, refresh_resp.text
        new_tok = refresh_resp.json().get("access_token")
        assert new_tok, "refresh did not return access_token"

        # Use the new token on a protected admin endpoint. /admin/roster
        # is the lightest one — pure read of the canonical seat map.
        roster_resp = await client.get(
            "/api/admin/roster",
            headers={"Authorization": f"Bearer {new_tok}"},
        )
        assert roster_resp.status_code == 200, (
            f"new access token from /refresh was rejected: "
            f"{roster_resp.status_code} {roster_resp.text}"
        )
