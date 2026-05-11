"""IBKR Web API thin client + admin routes.

Architecture:
    IBKR's unified Web API uses OAuth 2.0 — the operator pastes an
    access_token (and optionally account_id), Mission Control stores it
    Fernet-encrypted, and uses it as a Bearer token against
    api.ibkr.com. Wraps the same patterns we use for Kraken (probe
    before persist, redacted previews, execution-gate audit log) so the
    doctrine stays uniform.

Endpoints (operator-JWT authenticated):
    POST   /api/admin/ibkr/connect       Save encrypted token + probe.
    GET    /api/admin/ibkr/status        Connection summary (redacted).
    POST   /api/admin/ibkr/test          /v1/api/iserver/auth/status probe.
    POST   /api/admin/ibkr/tickle        Keep-alive (single tick).
    GET    /api/admin/ibkr/accounts      List portfolio accounts.
    GET    /api/admin/ibkr/positions     Read-only positions for the
                                         active account.
    DELETE /api/admin/ibkr/disconnect    Wipe credentials + stop tickler.
    POST   /api/admin/ibkr/execution     Flip the execution-allowed gate.
                                         Same confirmation-phrase guard
                                         as Kraken; trade endpoints are
                                         still NOT exposed by this
                                         router — the flag is groundwork.

Doctrine:
    - This client NEVER calls trade endpoints (`/iserver/account/.../orders`,
      `/iserver/reply/...`). Wiring those is a separate change that
      goes through the promotion / dual-sign gate.
    - `execution_enabled` defaults False on save; flipping is
      audit-logged with operator email + new state.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import IBKR_AUDIT_LOG, IBKR_CREDENTIALS
from shared.credentials import decrypt, encrypt, redact


logger = logging.getLogger("risedual.ibkr")
USER_AGENT = "risedual-mission-control/1.0"
DEFAULT_BASE_URL = "https://api.ibkr.com"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────── http client ────────────────────────

def _client(base_url: str, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
        },
        timeout=15.0,
    )


class IBKRError(Exception):
    def __init__(self, message: str, status: int = 0, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


async def _request(
    base_url: str, token: str, method: str, path: str,
    json_body: Optional[dict | list] = None,
) -> Any:
    async with _client(base_url, token) as c:
        try:
            r = await c.request(method, path, json=json_body)
        except httpx.HTTPError as e:
            raise IBKRError(f"transport: {e}") from e
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        data = r.text
    if r.status_code >= 400:
        raise IBKRError(
            f"HTTP {r.status_code}", status=r.status_code, body=data,
        )
    return data


# ──────────────────────── credential resolution ────────────────────────

async def get_active() -> Optional[dict]:
    """Returns {base_url, token (plaintext), account_id} or None."""
    doc = await db[IBKR_CREDENTIALS].find_one({"_id": "singleton"}, {"_id": 0})
    if not doc or not doc.get("encrypted_access_token"):
        return None
    try:
        tok = decrypt(doc["encrypted_access_token"])
    except ValueError:
        return None
    return {
        "base_url": doc.get("base_url") or DEFAULT_BASE_URL,
        "token": tok,
        "account_id": doc.get("account_id"),
    }


# ──────────────────────── probe ────────────────────────

async def probe(base_url: str, token: str) -> dict:
    """Probe auth + account list. Returns:
        {
          authenticated: bool,
          accounts: [{id, alias, type}, ...],
          auth_status: <raw /auth/status payload>,
          errors: {auth, accounts},
        }
    """
    result: dict[str, Any] = {
        "authenticated": False, "accounts": [], "auth_status": None,
        "errors": {},
    }
    try:
        auth = await _request(base_url, token, "GET", "/v1/api/iserver/auth/status")
        result["auth_status"] = auth
        result["authenticated"] = bool(
            isinstance(auth, dict) and auth.get("authenticated")
        )
    except IBKRError as e:
        result["errors"]["auth"] = f"HTTP {e.status}: {e.body}" if e.status else str(e)

    try:
        accts = await _request(base_url, token, "GET", "/v1/api/iserver/accounts")
        # Shape varies: may be {accounts:[...]} or list directly
        rows = (
            accts.get("accounts") if isinstance(accts, dict) else accts
        ) or []
        result["accounts"] = [
            {"id": a.get("id") or a.get("accountId"), "alias": a.get("accountAlias"), "type": a.get("type")}
            if isinstance(a, dict) else {"id": str(a)}
            for a in rows
        ]
    except IBKRError as e:
        result["errors"]["accounts"] = f"HTTP {e.status}: {e.body}" if e.status else str(e)

    return result


# ──────────────────────── tickler ────────────────────────

_TICKLE_TASK: asyncio.Task | None = None
_TICKLE_LAST = {"ts": None, "ok": False, "error": None}
_TICKLE_INTERVAL_SECONDS = 300  # 5 min — IBKR sessions otherwise time out


async def _tickle_once() -> None:
    active = await get_active()
    if not active:
        return
    try:
        await _request(active["base_url"], active["token"], "POST", "/v1/api/tickle")
        _TICKLE_LAST.update({"ts": _now_iso(), "ok": True, "error": None})
    except IBKRError as e:
        _TICKLE_LAST.update({"ts": _now_iso(), "ok": False, "error": str(e)})
        logger.warning("IBKR tickle failed: %s", e)


async def _tickle_loop() -> None:
    while True:
        try:
            await _tickle_once()
        except Exception as e:  # noqa: BLE001
            _TICKLE_LAST.update({"ts": _now_iso(), "ok": False, "error": f"loop: {e}"})
        await asyncio.sleep(_TICKLE_INTERVAL_SECONDS)


def start_tickler_if_needed() -> None:
    global _TICKLE_TASK
    if _TICKLE_TASK and not _TICKLE_TASK.done():
        return
    _TICKLE_TASK = asyncio.create_task(_tickle_loop(), name="ibkr-tickler")


async def stop_tickler() -> None:
    global _TICKLE_TASK
    if _TICKLE_TASK and not _TICKLE_TASK.done():
        _TICKLE_TASK.cancel()
        with suppress(asyncio.CancelledError):
            await _TICKLE_TASK
    _TICKLE_TASK = None


# ──────────────────────── audit ────────────────────────

async def _audit(action: str, actor: str, payload: dict | None = None) -> None:
    await db[IBKR_AUDIT_LOG].insert_one({
        "ts": _now_iso(),
        "action": action,
        "actor": actor,
        "payload": payload or {},
    })


# ──────────────────────── models ────────────────────────

class ConnectIn(BaseModel):
    access_token: str = Field(..., min_length=20, max_length=2048)
    account_id: Optional[str] = Field(default=None, max_length=64)
    base_url: str = Field(default=DEFAULT_BASE_URL, max_length=200)

    @field_validator("base_url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("base_url must use https://")
        return v.rstrip("/")


class ExecutionToggleIn(BaseModel):
    enabled: bool
    confirm: str = ""


# ──────────────────────── router ────────────────────────

router = APIRouter(prefix="/admin/ibkr", tags=["ibkr"])


@router.post("/connect")
async def connect(body: ConnectIn, user: dict = Depends(get_current_user)):
    """Probe-then-store. Refuses to persist if auth_status returns
    authenticated=false (typical when access_token is expired)."""
    pr = await probe(body.base_url, body.access_token)
    if not pr["authenticated"]:
        err = pr["errors"].get("auth") or "auth_status returned authenticated=false"
        raise HTTPException(
            status_code=400,
            detail=(
                f"IBKR keys rejected: {err}. Check: (1) access_token is "
                "current (IBKR tokens expire), (2) base_url is correct, "
                "(3) the gateway/SSO session has been initialised on the "
                "IBKR side at least once."
            ),
        )

    # Pick a default account_id if the operator didn't supply one and we
    # found exactly one account.
    account_id = body.account_id
    if not account_id and len(pr["accounts"]) == 1:
        account_id = pr["accounts"][0]["id"]

    encrypted = encrypt(body.access_token)
    now = _now_iso()
    doc = {
        "_id": "singleton",
        "base_url": body.base_url,
        "encrypted_access_token": encrypted,
        "token_preview": redact(body.access_token, 6),
        "account_id": account_id,
        "accounts": pr["accounts"],
        "auth_status": pr["auth_status"],
        "execution_enabled": False,
        "created_at": now,
        "updated_at": now,
        "connected_by": user.get("email") or "operator",
    }
    await db[IBKR_CREDENTIALS].replace_one({"_id": "singleton"}, doc, upsert=True)
    await _audit("ibkr_connect", user.get("email") or "operator", {"account_id": account_id})

    start_tickler_if_needed()
    asyncio.create_task(_tickle_once())

    return _public_status(doc)


@router.get("/status")
async def status(_user: dict = Depends(get_current_user)):
    doc = await db[IBKR_CREDENTIALS].find_one({"_id": "singleton"}, {"_id": 0})
    if not doc:
        return {
            "connected": False, "execution_enabled": False,
            "tickler_running": False,
        }
    return _public_status(doc)


def _public_status(doc: dict) -> dict:
    """Shape the singleton doc for UI consumption. Never leak the
    encrypted token past redaction."""
    return {
        "connected": True,
        "base_url": doc.get("base_url"),
        "token_preview": doc.get("token_preview"),
        "account_id": doc.get("account_id"),
        "accounts": doc.get("accounts", []),
        "auth_status": doc.get("auth_status"),
        "execution_enabled": doc.get("execution_enabled", False),
        "connected_by": doc.get("connected_by"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "tickler_running": bool(_TICKLE_TASK and not _TICKLE_TASK.done()),
        "last_tickle": _TICKLE_LAST,
    }


@router.post("/test")
async def test_(user: dict = Depends(get_current_user)):
    active = await get_active()
    if not active:
        raise HTTPException(status_code=404, detail="no IBKR credentials stored")
    try:
        auth = await _request(active["base_url"], active["token"], "GET", "/v1/api/iserver/auth/status")
    except IBKRError as e:
        raise HTTPException(status_code=502, detail=f"IBKR test failed: {e}") from e
    return {"ok": True, "auth_status": auth, "called_at": _now_iso()}


@router.post("/tickle")
async def tickle(user: dict = Depends(get_current_user)):
    active = await get_active()
    if not active:
        raise HTTPException(status_code=404, detail="no IBKR credentials stored")
    await _tickle_once()
    return {"ok": True, "last_tickle": _TICKLE_LAST}


@router.get("/accounts")
async def accounts(_user: dict = Depends(get_current_user)):
    active = await get_active()
    if not active:
        raise HTTPException(status_code=404, detail="no IBKR credentials stored")
    try:
        result = await _request(active["base_url"], active["token"], "GET", "/v1/api/iserver/accounts")
    except IBKRError as e:
        raise HTTPException(status_code=502, detail=f"IBKR accounts failed: {e}") from e
    return result


@router.get("/positions")
async def positions(
    page: int = 0,
    _user: dict = Depends(get_current_user),
):
    """Read-only positions for the active account."""
    active = await get_active()
    if not active or not active.get("account_id"):
        raise HTTPException(status_code=404, detail="no IBKR account configured")
    try:
        result = await _request(
            active["base_url"], active["token"],
            "GET", f"/v1/api/portfolio/{active['account_id']}/positions/{page}",
        )
    except IBKRError as e:
        raise HTTPException(status_code=502, detail=f"IBKR positions failed: {e}") from e
    return {"items": result if isinstance(result, list) else result, "account_id": active["account_id"]}


@router.delete("/disconnect")
async def disconnect(user: dict = Depends(get_current_user)):
    await db[IBKR_CREDENTIALS].delete_one({"_id": "singleton"})
    await stop_tickler()
    await _audit("ibkr_disconnect", user.get("email") or "operator", {})
    return {"ok": True}


@router.post("/execution")
async def toggle_execution(
    body: ExecutionToggleIn, user: dict = Depends(get_current_user),
):
    """Flip the execution-allowed gate. Defaults off. Trade endpoints
    are still not exposed by this router — this flag is groundwork."""
    expected = "I authorize execution on IBKR" if body.enabled else "Disable execution"
    if body.confirm != expected:
        raise HTTPException(
            status_code=400,
            detail=f"confirmation phrase mismatch — expected: {expected!r}",
        )
    doc = await db[IBKR_CREDENTIALS].find_one({"_id": "singleton"})
    if not doc:
        raise HTTPException(status_code=404, detail="no IBKR credentials stored")
    await db[IBKR_CREDENTIALS].update_one(
        {"_id": "singleton"},
        {"$set": {"execution_enabled": body.enabled, "updated_at": _now_iso()}},
    )
    await _audit(
        "ibkr_execution_toggle",
        user.get("email") or "operator",
        {"new_state": body.enabled},
    )
    doc = await db[IBKR_CREDENTIALS].find_one({"_id": "singleton"}, {"_id": 0})
    return _public_status(doc)


@router.get("/audit")
async def audit_log(
    limit: int = 50,
    _user: dict = Depends(get_current_user),
):
    rows = await db[IBKR_AUDIT_LOG].find({}, {"_id": 0}).sort("ts", -1).to_list(min(limit, 200))
    return {"items": rows, "count": len(rows)}
