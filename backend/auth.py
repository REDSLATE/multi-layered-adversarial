"""JWT auth (cookie-based) + admin seeding."""
import asyncio
import os
import uuid
import bcrypt
import jwt
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response, Depends
from pydantic import BaseModel, EmailStr

from db import db

JWT_ALGORITHM = "HS256"


def _secret() -> str:
    return os.environ["JWT_SECRET"]


def hash_password(password: str) -> str:
    """Synchronous bcrypt hash. CPU-bound (~1s at cost 12). Use the
    async wrappers below from inside FastAPI handlers — calling this
    directly on the event loop will FREEZE every concurrent request
    for the duration of the hash. This sync entry point exists for
    the seed/migration scripts only."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Synchronous bcrypt verify. Same warning as hash_password —
    DO NOT call from an async request handler. Use
    `verify_password_async` instead."""
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


# ── async wrappers (2026-06-08 event-loop fix) ──
#
# Why these exist: bcrypt.checkpw + bcrypt.hashpw are CPU-bound C
# calls that take 1-1.5s at the default cost factor. Called directly
# from `async def` handlers, they BLOCK the asyncio event loop for
# the full duration — meaning EVERY other in-flight request (brain
# heartbeats, dashboard polls, parallel logins) queues behind. With
# Cloudflare in front, a few backed-up requests trigger 520 errors
# and the operator-visible "site times out → 520 → refresh several
# times to log in" pattern.
#
# `asyncio.to_thread` runs the CPU work on a worker thread, freeing
# the event loop to keep serving requests during the hash.
async def hash_password_async(password: str) -> str:
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(plain: str, hashed: str) -> bool:
    return await asyncio.to_thread(verify_password, plain, hashed)


def _create_access(user_id: str, email: str) -> str:
    return jwt.encode(
        {
            "sub": user_id,
            "email": email,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=60),
            "type": "access",
        },
        _secret(),
        algorithm=JWT_ALGORITHM,
    )


def _create_refresh(user_id: str) -> str:
    return jwt.encode(
        {
            "sub": user_id,
            "exp": datetime.now(timezone.utc) + timedelta(days=7),
            "type": "refresh",
        },
        _secret(),
        algorithm=JWT_ALGORITHM,
    )


def _set_cookies(response: Response, access: str, refresh: str) -> None:
    response.set_cookie(
        "access_token", access, httponly=True, secure=True, samesite="none",
        max_age=3600, path="/",
    )
    response.set_cookie(
        "refresh_token", refresh, httponly=True, secure=True, samesite="none",
        max_age=7 * 24 * 3600, path="/",
    )


async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, _secret(), algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


# ---------- Models ----------
class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    role: str


# ---------- Routes ----------
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(req: LoginRequest, response: Response, request: Request):
    email = req.email.lower()
    ip = request.client.host if request.client else "unknown"
    identifier = f"{ip}:{email}"

    # Brute force: 5 fails in 15min = lockout.
    # `ts` is stored as a BSON Date (not ISO string) so the TTL index
    # in db.py can auto-prune rows older than the 15-min window.
    # count_documents capped at 5 — we only need to know if we've hit
    # the lockout threshold, not the precise count.
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
    fails = await db.login_attempts.count_documents(
        {
            "identifier": identifier,
            "success": False,
            "ts": {"$gte": cutoff},
        },
        limit=5,
    )
    if fails >= 5:
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again later.")

    user = await db.users.find_one({"email": email})
    pw_ok = False
    if user:
        # bcrypt verify runs in a worker thread so the event loop
        # stays unblocked while the ~1s CPU hash work runs. Without
        # this, EVERY concurrent in-flight request queues behind the
        # bcrypt — Cloudflare 520s the operator's parallel
        # auth/me + dashboard-poll requests.
        pw_ok = await verify_password_async(req.password, user["password_hash"])
    if not user or not pw_ok:
        await db.login_attempts.insert_one({
            "identifier": identifier,
            "ts": datetime.now(timezone.utc),
            "success": False,
        })
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Clear fails
    await db.login_attempts.delete_many({"identifier": identifier})

    access = _create_access(user["id"], user["email"])
    refresh = _create_refresh(user["id"])
    _set_cookies(response, access, refresh)
    return {
        "user": UserOut(id=user["id"], email=user["email"], name=user["name"], role=user["role"]).model_dump(),
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
    }


@router.get("/me", response_model=UserOut)
async def me(user: dict = Depends(get_current_user)):
    return UserOut(**user)


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    return {"ok": True}


@router.post("/refresh")
async def refresh(request: Request, response: Response):
    rt = request.cookies.get("refresh_token")
    if not rt:
        raise HTTPException(status_code=401, detail="No refresh token")
    try:
        payload = jwt.decode(rt, _secret(), algorithms=[JWT_ALGORITHM])
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid refresh token")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    user = await db.users.find_one({"id": payload["sub"]}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    new_access = _create_access(user["id"], user["email"])
    response.set_cookie("access_token", new_access, httponly=True, secure=True,
                        samesite="none", max_age=3600, path="/")
    return {"ok": True}


# ---------- Seeder ----------
async def seed_admin(database) -> None:
    email = os.environ.get("ADMIN_EMAIL", "admin@risedual.io").lower()
    password = os.environ.get("ADMIN_PASSWORD", "risedual-admin-2026")
    existing = await database.users.find_one({"email": email})
    if existing is None:
        # bcrypt off the event loop — see hash_password_async docstring.
        pw_hash = await hash_password_async(password)
        await database.users.insert_one({
            "id": str(uuid.uuid4()),
            "email": email,
            "password_hash": pw_hash,
            "name": "RISEDUAL Admin",
            "role": "admin",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    elif not await verify_password_async(password, existing["password_hash"]):
        pw_hash = await hash_password_async(password)
        await database.users.update_one(
            {"email": email},
            {"$set": {"password_hash": pw_hash}},
        )
