"""Public.com retail brokerage API thin client + admin routes.

Auth flow (per https://public.com/api/docs/quickstart):
    1. Operator generates a long-lived SECRET KEY at
       public.com/settings/security/api.
    2. We exchange the secret for a short-lived ACCESS TOKEN via
       POST /userapiauthservice/personal/access-tokens with
       {validityInMinutes, secret}.
    3. The access token is used as `Authorization: Bearer ...` for all
       trading endpoints.

Mission Control stores the secret key Fernet-encrypted (same scheme as
Kraken/IBKR) and caches the active access token + its expiry. A
background refresher proactively rolls the access token before expiry
so live operator calls never have to wait on the exchange.

Endpoints (operator-JWT authenticated):
    POST   /api/admin/public/connect        Save encrypted secret + probe.
    GET    /api/admin/public/status         Connection summary (redacted).
    POST   /api/admin/public/test           Account-probe call.
    POST   /api/admin/public/refresh-token  Force token refresh.
    GET    /api/admin/public/accounts       List brokerage accounts.
    GET    /api/admin/public/portfolio      Positions + balances for active account.
    DELETE /api/admin/public/disconnect     Wipe credentials + stop refresher.
    POST   /api/admin/public/execution      Flip execution-allowed gate.
                                            Same confirmation-phrase guard
                                            as Kraken/IBKR. Trade endpoints
                                            are NOT exposed by this router.
    GET    /api/admin/public/audit          Append-only action log.

Doctrine:
    - This client NEVER calls order placement endpoints
      (`/userapigateway/trading/.../order/v2`). Trade wiring is a
      separate change that flows through the promotion / dual-sign gate.
    - `execution_enabled` defaults False; flipping it is audit-logged
      with operator email + new state. The flag exists as groundwork;
      Phase 2 will wire the actual trade endpoints behind it.
    - Public.com has no PDT restrictions for cash accounts — this slot
      can be the day-trade venue for the executor brain once Phase 2
      ships, alongside Kraken (crypto, no PDT) and IBKR (margin/PDT).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import PUBLIC_AUDIT_LOG, PUBLIC_CREDENTIALS
from shared.credentials import decrypt, encrypt, redact


logger = logging.getLogger("risedual.public")
USER_AGENT = "risedual-mission-control/1.0"
DEFAULT_BASE_URL = "https://api.public.com"

# Access tokens are operator-configurable in minutes. We default to a
# 24-hour validity and refresh 5 minutes before expiry. The refresher
# polls once per minute; that's plenty fine-grained for a 24h token.
DEFAULT_TOKEN_VALIDITY_MINUTES = 1440
REFRESH_BUFFER_SECONDS = 300        # refresh when ≤ 5 min remain
REFRESH_POLL_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


# ──────────────────────── http client ────────────────────────

class PublicError(Exception):
    def __init__(self, message: str, status: int = 0, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _client(base_url: str, token: Optional[str] = None) -> httpx.AsyncClient:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.AsyncClient(base_url=base_url, headers=headers, timeout=15.0)


async def _request(
    base_url: str, token: Optional[str], method: str, path: str,
    json_body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> Any:
    async with _client(base_url, token) as c:
        try:
            r = await c.request(method, path, json=json_body, params=params)
        except httpx.HTTPError as e:
            raise PublicError(f"transport: {e}") from e
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        data = r.text
    if r.status_code >= 400:
        raise PublicError(f"HTTP {r.status_code}", status=r.status_code, body=data)
    return data


# ──────────────────────── token exchange ────────────────────────

async def exchange_secret_for_access_token(
    base_url: str, secret: str, validity_minutes: int = DEFAULT_TOKEN_VALIDITY_MINUTES,
) -> dict:
    """POST /userapiauthservice/personal/access-tokens.

    Returns {access_token, expires_at_iso, validity_minutes}.
    Raises PublicError on auth or transport failure.
    """
    body = {"validityInMinutes": int(validity_minutes), "secret": secret}
    result = await _request(
        base_url, None, "POST",
        "/userapiauthservice/personal/access-tokens", body,
    )
    if not isinstance(result, dict) or "accessToken" not in result:
        raise PublicError(
            f"unexpected token response: {str(result)[:120]}",
            status=200, body=result,
        )
    expires_at = _now() + timedelta(minutes=validity_minutes)
    return {
        "access_token": result["accessToken"],
        "expires_at_iso": expires_at.isoformat(),
        "validity_minutes": validity_minutes,
    }


# ──────────────────────── credential resolution ────────────────────────

async def _stored_doc() -> Optional[dict]:
    return await db[PUBLIC_CREDENTIALS].find_one({"_id": "singleton"}, {"_id": 0})


async def _ensure_fresh_access_token(doc: dict) -> tuple[str, str, str]:
    """Returns (base_url, access_token, account_id). Refreshes the
    access token if it is missing or within REFRESH_BUFFER_SECONDS of
    expiry. Persists the refreshed token back into the singleton doc.
    Raises PublicError if no secret or refresh fails.
    """
    base_url = doc.get("base_url") or DEFAULT_BASE_URL
    try:
        secret = decrypt(doc["encrypted_secret"])
    except (KeyError, ValueError) as e:
        raise PublicError("stored secret is missing or unreadable") from e

    expires_at_iso = doc.get("access_token_expires_at")
    enc_tok = doc.get("encrypted_access_token")
    needs_refresh = True
    if expires_at_iso and enc_tok:
        try:
            expires_at = datetime.fromisoformat(expires_at_iso)
            if expires_at - _now() > timedelta(seconds=REFRESH_BUFFER_SECONDS):
                needs_refresh = False
        except ValueError:
            needs_refresh = True

    if needs_refresh:
        validity = int(doc.get("token_validity_minutes") or DEFAULT_TOKEN_VALIDITY_MINUTES)
        new = await exchange_secret_for_access_token(base_url, secret, validity)
        await db[PUBLIC_CREDENTIALS].update_one(
            {"_id": "singleton"},
            {"$set": {
                "encrypted_access_token": encrypt(new["access_token"]),
                "access_token_expires_at": new["expires_at_iso"],
                "access_token_refreshed_at": _now_iso(),
                "updated_at": _now_iso(),
            }},
        )
        access_token = new["access_token"]
    else:
        try:
            access_token = decrypt(enc_tok)
        except ValueError as e:
            raise PublicError("stored access token unreadable") from e

    return base_url, access_token, doc.get("account_id")


async def get_active() -> Optional[dict]:
    """Decrypt + refresh-if-needed. Returns {base_url, access_token,
    account_id} or None if no creds stored. Raises PublicError on
    refresh failure (so callers know the cause)."""
    doc = await _stored_doc()
    if not doc or not doc.get("encrypted_secret"):
        return None
    base_url, access_token, account_id = await _ensure_fresh_access_token(doc)
    return {"base_url": base_url, "access_token": access_token, "account_id": account_id}


# ──────────────────────── probe ────────────────────────

async def probe(base_url: str, secret: str, validity_minutes: int) -> dict:
    """Probe: exchange secret → list accounts.

    Returns:
      {
        token_ok: bool,
        access_token: <plaintext, returned to caller for persistence>,
        expires_at_iso, validity_minutes,
        accounts: [...], errors: {token, accounts}
      }
    """
    result: dict[str, Any] = {
        "token_ok": False, "access_token": None, "expires_at_iso": None,
        "validity_minutes": validity_minutes, "accounts": [], "errors": {},
    }
    try:
        tok = await exchange_secret_for_access_token(base_url, secret, validity_minutes)
        result.update({
            "token_ok": True,
            "access_token": tok["access_token"],
            "expires_at_iso": tok["expires_at_iso"],
        })
    except PublicError as e:
        result["errors"]["token"] = f"HTTP {e.status}: {e.body}" if e.status else str(e)
        return result   # no token → no further probing possible

    try:
        accts = await _request(
            base_url, result["access_token"], "GET",
            "/userapigateway/trading/account",
        )
        rows = accts.get("accounts") if isinstance(accts, dict) else accts
        result["accounts"] = [
            {
                "id": a.get("accountId"),
                "type": a.get("accountType"),
                "brokerage_type": a.get("brokerageAccountType"),
                "options_level": a.get("optionsLevel"),
                "permissions": a.get("tradePermissions"),
            }
            for a in (rows or [])
            if isinstance(a, dict)
        ]
    except PublicError as e:
        result["errors"]["accounts"] = f"HTTP {e.status}: {e.body}" if e.status else str(e)

    return result


# ──────────────────────── refresher ────────────────────────

_REFRESH_TASK: asyncio.Task | None = None
_REFRESH_LAST = {"ts": None, "ok": False, "error": None}


async def _refresh_once() -> None:
    doc = await _stored_doc()
    if not doc or not doc.get("encrypted_secret"):
        return
    try:
        await _ensure_fresh_access_token(doc)
        _REFRESH_LAST.update({"ts": _now_iso(), "ok": True, "error": None})
    except PublicError as e:
        _REFRESH_LAST.update({"ts": _now_iso(), "ok": False, "error": str(e)})
        logger.warning("Public token refresh failed: %s", e)


async def _refresh_loop() -> None:
    while True:
        try:
            await _refresh_once()
        except Exception as e:  # noqa: BLE001
            _REFRESH_LAST.update({"ts": _now_iso(), "ok": False, "error": f"loop: {e}"})
        await asyncio.sleep(REFRESH_POLL_SECONDS)


def start_refresher_if_needed() -> None:
    global _REFRESH_TASK
    if _REFRESH_TASK and not _REFRESH_TASK.done():
        return
    _REFRESH_TASK = asyncio.create_task(_refresh_loop(), name="public-refresher")


async def stop_refresher() -> None:
    global _REFRESH_TASK
    if _REFRESH_TASK and not _REFRESH_TASK.done():
        _REFRESH_TASK.cancel()
        with suppress(asyncio.CancelledError):
            await _REFRESH_TASK
    _REFRESH_TASK = None


# ──────────────────────── audit ────────────────────────

async def _audit(action: str, actor: str, payload: dict | None = None) -> None:
    await db[PUBLIC_AUDIT_LOG].insert_one({
        "ts": _now_iso(),
        "action": action,
        "actor": actor,
        "payload": payload or {},
    })


# ──────────────────────── models ────────────────────────

class ConnectIn(BaseModel):
    secret: str = Field(..., min_length=20, max_length=2048)
    account_id: Optional[str] = Field(default=None, max_length=64)
    base_url: str = Field(default=DEFAULT_BASE_URL, max_length=200)
    token_validity_minutes: int = Field(
        default=DEFAULT_TOKEN_VALIDITY_MINUTES, ge=5, le=10080,  # 5 min … 7 days
    )

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

router = APIRouter(prefix="/admin/public", tags=["public"])


@router.post("/connect")
async def connect(body: ConnectIn, user: dict = Depends(get_current_user)):
    """Probe (exchange secret → access token → account list), then
    persist the encrypted secret + cache the access token. Refuses to
    persist if the token exchange fails."""
    pr = await probe(body.base_url, body.secret, body.token_validity_minutes)
    if not pr["token_ok"]:
        err = pr["errors"].get("token") or "token exchange failed"
        raise HTTPException(
            status_code=400,
            detail=(
                f"Public.com secret rejected: {err}. Check: (1) the "
                "secret was copied from public.com/settings/security/api "
                "without trailing whitespace, (2) the secret is active "
                "(rotate at Public.com if unsure), (3) the base_url is "
                "correct."
            ),
        )

    # Default-pick the active account when there's exactly one.
    account_id = body.account_id
    if not account_id and len(pr["accounts"]) == 1:
        account_id = pr["accounts"][0]["id"]

    now = _now_iso()
    doc = {
        "_id": "singleton",
        "base_url": body.base_url,
        "encrypted_secret": encrypt(body.secret),
        "secret_preview": redact(body.secret, 6),
        "encrypted_access_token": encrypt(pr["access_token"]),
        "access_token_expires_at": pr["expires_at_iso"],
        "access_token_refreshed_at": now,
        "token_validity_minutes": body.token_validity_minutes,
        "account_id": account_id,
        "accounts": pr["accounts"],
        "execution_enabled": False,
        "created_at": now,
        "updated_at": now,
        "connected_by": user.get("email") or "operator",
    }
    await db[PUBLIC_CREDENTIALS].replace_one(
        {"_id": "singleton"}, doc, upsert=True,
    )
    await _audit("public_connect", user.get("email") or "operator", {"account_id": account_id})

    start_refresher_if_needed()
    return _public_status(doc)


@router.get("/status")
async def status(_user: dict = Depends(get_current_user)):
    doc = await _stored_doc()
    if not doc:
        return {
            "connected": False, "execution_enabled": False,
            "refresher_running": False,
        }
    return _public_status(doc)


def _public_status(doc: dict) -> dict:
    """Shape the singleton doc for UI consumption. Never returns the
    encrypted secret or any plaintext token."""
    return {
        "connected": True,
        "base_url": doc.get("base_url"),
        "secret_preview": doc.get("secret_preview"),
        "account_id": doc.get("account_id"),
        "accounts": doc.get("accounts", []),
        "token_validity_minutes": doc.get("token_validity_minutes"),
        "access_token_expires_at": doc.get("access_token_expires_at"),
        "access_token_refreshed_at": doc.get("access_token_refreshed_at"),
        "execution_enabled": doc.get("execution_enabled", False),
        "connected_by": doc.get("connected_by"),
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "refresher_running": bool(_REFRESH_TASK and not _REFRESH_TASK.done()),
        "last_refresh": _REFRESH_LAST,
    }


@router.post("/test")
async def test_(_user: dict = Depends(get_current_user)):
    try:
        active = await get_active()
    except PublicError as e:
        raise HTTPException(status_code=502, detail=f"Public refresh failed: {e}") from e
    if not active:
        raise HTTPException(status_code=404, detail="no Public.com credentials stored")
    try:
        accts = await _request(
            active["base_url"], active["access_token"], "GET",
            "/userapigateway/trading/account",
        )
    except PublicError as e:
        raise HTTPException(status_code=502, detail=f"Public test failed: {e}") from e
    return {"ok": True, "accounts": accts, "called_at": _now_iso()}


@router.post("/refresh-token")
async def refresh_token(user: dict = Depends(get_current_user)):
    doc = await _stored_doc()
    if not doc:
        raise HTTPException(status_code=404, detail="no Public.com credentials stored")
    # Force refresh by clearing the cached token.
    await db[PUBLIC_CREDENTIALS].update_one(
        {"_id": "singleton"},
        {"$set": {"access_token_expires_at": None}},
    )
    await _refresh_once()
    await _audit("public_refresh_token", user.get("email") or "operator")
    return _public_status(await _stored_doc())


@router.get("/accounts")
async def accounts(_user: dict = Depends(get_current_user)):
    try:
        active = await get_active()
    except PublicError as e:
        raise HTTPException(status_code=502, detail=f"Public refresh failed: {e}") from e
    if not active:
        raise HTTPException(status_code=404, detail="no Public.com credentials stored")
    try:
        result = await _request(
            active["base_url"], active["access_token"], "GET",
            "/userapigateway/trading/account",
        )
    except PublicError as e:
        raise HTTPException(status_code=502, detail=f"Public accounts failed: {e}") from e
    return result


@router.get("/portfolio")
async def portfolio(_user: dict = Depends(get_current_user)):
    """Positions + balances for the active account."""
    try:
        active = await get_active()
    except PublicError as e:
        raise HTTPException(status_code=502, detail=f"Public refresh failed: {e}") from e
    if not active or not active.get("account_id"):
        raise HTTPException(status_code=404, detail="no Public.com account configured")
    try:
        result = await _request(
            active["base_url"], active["access_token"], "GET",
            f"/userapigateway/trading/{active['account_id']}/portfolio/v2",
        )
    except PublicError as e:
        raise HTTPException(status_code=502, detail=f"Public portfolio failed: {e}") from e
    return {"account_id": active["account_id"], "portfolio": result}


@router.delete("/disconnect")
async def disconnect(user: dict = Depends(get_current_user)):
    await db[PUBLIC_CREDENTIALS].delete_one({"_id": "singleton"})
    await stop_refresher()
    await _audit("public_disconnect", user.get("email") or "operator", {})
    return {"ok": True}


@router.post("/execution")
async def toggle_execution(
    body: ExecutionToggleIn, user: dict = Depends(get_current_user),
):
    """Flip the execution-allowed gate. Defaults off. Trade endpoints
    are still NOT exposed by this router — this flag is groundwork."""
    expected = "I authorize execution on Public" if body.enabled else "Disable execution"
    if body.confirm != expected:
        raise HTTPException(
            status_code=400,
            detail=f"confirmation phrase mismatch — expected: {expected!r}",
        )
    doc = await _stored_doc()
    if not doc:
        raise HTTPException(status_code=404, detail="no Public.com credentials stored")
    await db[PUBLIC_CREDENTIALS].update_one(
        {"_id": "singleton"},
        {"$set": {"execution_enabled": body.enabled, "updated_at": _now_iso()}},
    )
    await _audit(
        "public_execution_toggle",
        user.get("email") or "operator",
        {"new_state": body.enabled},
    )
    return _public_status(await _stored_doc())


@router.get("/audit")
async def audit_log(
    limit: int = 50,
    _user: dict = Depends(get_current_user),
):
    rows = await db[PUBLIC_AUDIT_LOG].find({}, {"_id": 0}).sort("ts", -1).to_list(min(limit, 200))
    return {"items": rows, "count": len(rows)}
