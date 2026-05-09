"""Stack-specific dependency injection for Code Evolution.

This file is the ONLY place each stack edits when wiring the patch-kit in.
The package itself imports `get_current_user` and `get_dispatcher` from here;
the host stack supplies the implementations.

Default behaviour (out of the box): NotImplementedError. Each stack must
replace the two functions below with its own auth + storage glue.

────────────────────────────────────────────────────────────────────────
Example wiring for a stack with FastAPI + JWT-cookie auth + Motor + Mongo:

    from fastapi import Request, HTTPException
    from motor.motor_asyncio import AsyncIOMotorClient
    import os, jwt
    from .receipts import MotorDispatcher

    _client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    _db = _client[os.environ["DB_NAME"]]
    _dispatcher = MotorDispatcher(_db)

    async def get_current_user(request: Request) -> dict:
        token = request.cookies.get("access_token") or ""
        if token.startswith("Bearer "):
            token = token[7:]
        if not token:
            raise HTTPException(401, "Not authenticated")
        try:
            payload = jwt.decode(token, os.environ["JWT_SECRET"], algorithms=["HS256"])
        except Exception:                # noqa: BLE001
            raise HTTPException(401, "Invalid token")
        return {"id": payload["sub"], "email": payload["email"]}

    async def get_dispatcher():
        return _dispatcher
────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

from typing import Any

from .receipts import ReceiptDispatcher


async def get_current_user() -> dict[str, Any]:  # pragma: no cover
    raise NotImplementedError(
        "Code Evolution: replace deps.get_current_user with your stack's auth."
    )


async def get_dispatcher() -> ReceiptDispatcher:  # pragma: no cover
    raise NotImplementedError(
        "Code Evolution: replace deps.get_dispatcher with your stack's "
        "ReceiptDispatcher (MotorDispatcher for Mongo, or a custom impl)."
    )
