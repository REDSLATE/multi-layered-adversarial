"""Alpaca paper-trading admin routes — store keys, ping, manage.

Pattern mirrors `shared/kraken_routes.py`:
  * Keys are Fernet-encrypted at rest (`shared/credentials.py`).
  * UI ever sees only redacted previews — never plaintext after save.
  * Connect probes Alpaca BEFORE persisting. If the keys don't work, we
    don't store them.
  * Account row stored in `alpaca_credentials` as a singleton doc.
  * Every state-changing action is appended to `alpaca_audit_log`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import ALPACA_AUDIT_LOG, ALPACA_CREDENTIALS
from shared.broker.alpaca import AlpacaPaperAdapter
from shared.credentials import decrypt, encrypt, redact


router = APIRouter(prefix="/admin/alpaca", tags=["alpaca"])

_SINGLETON_ID = "singleton"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ───────────────────────────── audit ─────────────────────────────

async def _audit(action: str, actor: str, payload: dict | None = None) -> None:
    await db[ALPACA_AUDIT_LOG].insert_one({
        "ts": _now_iso(),
        "action": action,
        "actor": actor,
        "payload": payload or {},
    })


# ───────────────────────────── credential helpers ─────────────────────────────

async def get_alpaca_adapter() -> Optional[AlpacaPaperAdapter]:
    """Return a live adapter or None if no credentials are configured.

    Importable from anywhere — broker/execution code calls this to get
    the active adapter without knowing where keys live.
    """
    doc = await db[ALPACA_CREDENTIALS].find_one(
        {"_id": _SINGLETON_ID}, {"_id": 0, "api_key_enc": 1, "secret_key_enc": 1}
    )
    if not doc:
        return None
    try:
        api_key = decrypt(doc["api_key_enc"])
        secret_key = decrypt(doc["secret_key_enc"])
    except Exception:  # noqa: BLE001 - bad ciphertext / rotated key
        return None
    return AlpacaPaperAdapter(api_key, secret_key)


# ───────────────────────────── schemas ─────────────────────────────

class ConnectIn(BaseModel):
    api_key_id: str = Field(..., min_length=16, max_length=80, description="Alpaca API Key ID (paper)")
    secret_key: str = Field(..., min_length=16, max_length=120, description="Alpaca API Secret Key")

    @field_validator("api_key_id", "secret_key")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


# ───────────────────────────── public-status shape ─────────────────────────────

def _public_status(doc: dict, ping_snapshot: Optional[dict] = None) -> dict:
    """Shape singleton doc for UI consumption. Never leaks ciphertext."""
    return {
        "connected": True,
        "api_key_preview": doc.get("api_key_preview"),
        "secret_key_preview": doc.get("secret_key_preview"),
        "paper": True,
        "endpoint": "https://paper-api.alpaca.markets",
        "execution_enabled": doc.get("execution_enabled", True),
        "account_number": doc.get("account_number"),
        "last_equity_snapshot": doc.get("last_equity_snapshot"),
        "connected_by": doc.get("connected_by"),
        "connected_at": doc.get("connected_at"),
        "last_ping_at": doc.get("last_ping_at"),
        "last_ping_ok": doc.get("last_ping_ok"),
        "ping": ping_snapshot,
    }


# ───────────────────────────── endpoints ─────────────────────────────

@router.post("/connect")
async def connect(body: ConnectIn, user: dict = Depends(get_current_user)):
    """Store encrypted keys after a successful ping. Idempotent — calling
    again rotates the stored key pair."""
    # Probe BEFORE persisting. If keys don't work, we don't store them.
    try:
        adapter = AlpacaPaperAdapter(body.api_key_id, body.secret_key)
        ping = await adapter.ping()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=400,
            detail=(
                f"Alpaca rejected the keys: {e}. Common causes: "
                "(1) used live keys instead of paper, "
                "(2) typo in API Key ID / Secret, "
                "(3) account suspended."
            ),
        ) from e

    now = _now_iso()
    doc = {
        "_id": _SINGLETON_ID,
        "api_key_enc": encrypt(body.api_key_id),
        "secret_key_enc": encrypt(body.secret_key),
        "api_key_preview": redact(body.api_key_id, 4),
        "secret_key_preview": redact(body.secret_key, 4),
        "account_number": ping.get("account_number"),
        "last_equity_snapshot": ping.get("equity"),
        "execution_enabled": True,  # paper-only; safe default
        "connected_at": now,
        "connected_by": user.get("email") or "operator",
        "last_ping_at": now,
        "last_ping_ok": True,
        "updated_at": now,
    }
    await db[ALPACA_CREDENTIALS].replace_one({"_id": _SINGLETON_ID}, doc, upsert=True)
    await _audit("alpaca_connect", user.get("email") or "operator", {
        "account_number": doc["account_number"],
    })
    return _public_status(doc, ping_snapshot=ping)


@router.get("/status")
async def status(_user: dict = Depends(get_current_user)):
    doc = await db[ALPACA_CREDENTIALS].find_one({"_id": _SINGLETON_ID}, {"_id": 0})
    if not doc:
        return {"connected": False, "paper": True, "execution_enabled": False}
    return _public_status(doc)


@router.post("/test")
async def test(user: dict = Depends(get_current_user)):
    """Cheap pings the broker. Updates last_ping_* on the singleton doc."""
    adapter = await get_alpaca_adapter()
    if not adapter:
        raise HTTPException(status_code=404, detail="Alpaca not connected")
    try:
        ping = await adapter.ping()
        ok = True
    except Exception as e:  # noqa: BLE001
        await db[ALPACA_CREDENTIALS].update_one(
            {"_id": _SINGLETON_ID},
            {"$set": {"last_ping_at": _now_iso(), "last_ping_ok": False, "last_ping_error": str(e)}},
        )
        raise HTTPException(status_code=502, detail=f"Alpaca ping failed: {e}") from e

    await db[ALPACA_CREDENTIALS].update_one(
        {"_id": _SINGLETON_ID},
        {"$set": {
            "last_ping_at": _now_iso(),
            "last_ping_ok": ok,
            "last_equity_snapshot": ping.get("equity"),
            "account_number": ping.get("account_number"),
            "last_ping_error": None,
        }},
    )
    await _audit("alpaca_ping", user.get("email") or "operator", {"equity": ping.get("equity")})
    return {"ok": True, "ping": ping}


@router.get("/account")
async def account(_user: dict = Depends(get_current_user)):
    adapter = await get_alpaca_adapter()
    if not adapter:
        raise HTTPException(status_code=404, detail="Alpaca not connected")
    try:
        return await adapter.get_account()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/positions")
async def positions(_user: dict = Depends(get_current_user)):
    adapter = await get_alpaca_adapter()
    if not adapter:
        raise HTTPException(status_code=404, detail="Alpaca not connected")
    try:
        items = await adapter.list_positions()
        return {"items": items, "count": len(items)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/orders")
async def open_orders(_user: dict = Depends(get_current_user)):
    adapter = await get_alpaca_adapter()
    if not adapter:
        raise HTTPException(status_code=404, detail="Alpaca not connected")
    try:
        items = await adapter.list_open_orders()
        return {"items": items, "count": len(items)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.delete("/orders/{order_id}")
async def cancel_order_endpoint(order_id: str, user: dict = Depends(get_current_user)):
    adapter = await get_alpaca_adapter()
    if not adapter:
        raise HTTPException(status_code=404, detail="Alpaca not connected")
    try:
        await adapter.cancel_order(order_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    await _audit("alpaca_cancel_order", user.get("email") or "operator", {"order_id": order_id})
    return {"ok": True, "order_id": order_id}


@router.delete("/positions/{symbol}")
async def close_position_endpoint(symbol: str, user: dict = Depends(get_current_user)):
    adapter = await get_alpaca_adapter()
    if not adapter:
        raise HTTPException(status_code=404, detail="Alpaca not connected")
    try:
        order = await adapter.close_position(symbol)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e)) from e
    await _audit("alpaca_close_position", user.get("email") or "operator", {"symbol": symbol})
    return {"ok": True, "order": order}


@router.delete("/disconnect")
async def disconnect(user: dict = Depends(get_current_user)):
    await db[ALPACA_CREDENTIALS].delete_one({"_id": _SINGLETON_ID})
    await _audit("alpaca_disconnect", user.get("email") or "operator", {})
    return {"ok": True}


@router.get("/audit")
async def audit_log(limit: int = 50, _user: dict = Depends(get_current_user)):
    rows = (
        await db[ALPACA_AUDIT_LOG]
        .find({}, {"_id": 0})
        .sort("ts", -1)
        .to_list(min(limit, 500))
    )
    return {"items": rows, "count": len(rows)}
