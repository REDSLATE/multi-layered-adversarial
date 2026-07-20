"""Webull broker connection routes (2026-02-17).

Endpoints (all operator-JWT authenticated):
    POST   /api/admin/webull/connect      Validate structurally, persist
                                            encrypted to Mongo, hydrate
                                            in-process env.
    GET    /api/admin/webull/status       Redacted connection summary.
    POST   /api/admin/webull/probe        Check that the persisted keys
                                            produce a working token via
                                            the existing token-status
                                            endpoint.
    DELETE /api/admin/webull/disconnect   Wipe singleton + clear env.

Doctrine (mirrors Kraken Connect):
    - Plaintext `app_secret` only exists in memory long enough to be
      Fernet-encrypted for persistence.
    - The stored ciphertext never leaves the backend.
    - UI reads back a redacted preview only.

NOT covered here (out of scope):
    - The 2FA-derived `x-access-token` — that continues to live in the
      existing `webull_token` collection + `POST /api/admin/trader/
      webull-token-create` flow. The token layer is REQUIRED for quote
      + trade endpoints; the app_key / app_secret this file manages is
      the LOWER layer (needed to even start the 2FA push).

Why no live network probe here (2026-02-17):
    Webull's OpenAPI (`api.webull.com`) requires HMAC-SHA1 signed
    requests AND the 2FA-derived x-access-token for every meaningful
    call. The two endpoints that only need app_key+app_secret
    (`/openapi/auth/token/create`) trigger a mobile push — an
    unacceptable side effect for a "test connection" click. Legacy
    unsigned probe URLs (`u1strade.webullbroker.com`) return DNS
    failures from this deploy (host deprecated).
    Bottom line: real validation happens the first time the operator
    clicks "init token" in the Webull 2FA strip — that call fails
    loudly with 401 if the app_key/app_secret are wrong. Attempting
    a pre-token probe here would add code that either lies (skipped)
    or spams the operator's phone (2FA push). Structural validation
    (length bounds, required fields) is what this endpoint enforces.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import db
from namespaces import WEBULL_AUDIT_LOG, WEBULL_CREDENTIALS
from shared.credentials import encrypt, redact
from shared.webull_credentials import (
    clear_env,
    hydrate_env_from_mongo,
    push_creds_into_env,
)


logger = logging.getLogger("webull_credentials.routes")

router = APIRouter(prefix="/admin/webull", tags=["webull-credentials"])


# Region / environment enums — kept in sync with what the trader honors.
# `pro` = live money (MC always selects this per the 2026-02-19
# operator directive), `paper` = Webull's paper account. Region
# governs the API host routing; the SDK derives host from these
# values. `paper` remains in the enum ONLY because Webull's API
# rejects requests that omit an environment; MC never writes it.
_ALLOWED_REGIONS = {"us", "hk", "jp"}
_ALLOWED_ENVIRONMENTS = {"pro", "paper"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _audit(event: str, actor: str, extra: dict) -> None:
    """Append-only audit trail. Never raises."""
    try:
        await db[WEBULL_AUDIT_LOG].insert_one({
            "event": event,
            "actor": actor,
            "ts": _now_iso(),
            **extra,
        })
    except Exception as e:  # noqa: BLE001
        logger.warning("webull audit write failed: %s", e)


class ConnectIn(BaseModel):
    app_key: str = Field(..., min_length=16, max_length=200)
    app_secret: str = Field(..., min_length=16, max_length=400)
    account_id: str = Field(..., min_length=4, max_length=64)
    region_id: str = "us"
    environment: str = "pro"

    @field_validator("region_id")
    @classmethod
    def _region_known(cls, v: str) -> str:
        v2 = (v or "").strip().lower()
        if v2 not in _ALLOWED_REGIONS:
            raise ValueError(f"region_id must be one of {sorted(_ALLOWED_REGIONS)}")
        return v2

    @field_validator("environment")
    @classmethod
    def _env_known(cls, v: str) -> str:
        v2 = (v or "").strip().lower()
        if v2 not in _ALLOWED_ENVIRONMENTS:
            raise ValueError(f"environment must be one of {sorted(_ALLOWED_ENVIRONMENTS)}")
        # 2026-02-19 operator directive (LIVE ONLY): reject any
        # attempt to persist a paper-account credential. Webull's
        # `paper` env is retained in the enum for API-schema
        # compatibility, but MC never persists it.
        if v2 == "paper":
            raise ValueError(
                "environment='paper' is not permitted by operator "
                "directive (2026-02-19). Use 'pro' for live money.",
            )
        return v2


@router.post("/connect")
async def connect(body: ConnectIn, user: dict = Depends(get_current_user)):
    """Persist encrypted, hydrate env. See module docstring for why we
    don't attempt a live pre-token probe here — real validation lands
    on the operator's next `POST /api/admin/trader/webull-token-create`
    click, which fails loudly with 401 if these keys are wrong."""
    encrypted_secret = encrypt(body.app_secret.strip())
    now = _now_iso()
    doc = {
        "_id": "singleton",
        "app_key": body.app_key.strip(),
        "app_key_preview": redact(body.app_key.strip(), 6),
        "app_secret_preview": redact(body.app_secret.strip(), 4),
        "encrypted_app_secret": encrypted_secret,
        "account_id": body.account_id.strip(),
        "account_id_preview": redact(body.account_id.strip(), 4),
        "region_id": body.region_id,
        "environment": body.environment,
        "last_probe": None,   # set by /probe after operator triggers 2FA
        "created_at": now,
        "updated_at": now,
        "connected_by": user.get("email") or "operator",
    }
    await db[WEBULL_CREDENTIALS].replace_one({"_id": "singleton"}, doc, upsert=True)
    await _audit(
        "webull_connect",
        user.get("email") or "operator",
        {"account_id_preview": doc["account_id_preview"],
         "region_id": body.region_id, "environment": body.environment},
    )

    # Hot-hydrate the running process so the trader threads pick up
    # the new keys on the next tick — no supervisor restart required.
    push_creds_into_env(
        body.app_key.strip(),
        body.app_secret.strip(),
        body.account_id.strip(),
        body.region_id,
        body.environment,
    )

    return _public_status(doc)


@router.get("/status")
async def status(_user: dict = Depends(get_current_user)):
    doc = await db[WEBULL_CREDENTIALS].find_one({"_id": "singleton"}, {"_id": 0})
    if not doc:
        # Reveal whether env-based creds are present as a fallback — the
        # UI uses this to decide "do we have any Webull creds at all?"
        # without leaking values.
        env_configured = bool(
            (os.environ.get("WEBULL_APP_KEY") or "").strip()
            and (os.environ.get("WEBULL_APP_SECRET") or "").strip()
            and (os.environ.get("WEBULL_ACCOUNT_ID") or "").strip()
        )
        return {
            "connected": False,
            "env_configured": env_configured,
            "cred_source": "env" if env_configured else "none",
        }
    return _public_status(doc)


@router.post("/probe")
async def reprobe(_user: dict = Depends(get_current_user)):
    """Report the current Webull token status — the closest thing to a
    "does auth work" signal without triggering a fresh 2FA push. Also
    stamps `last_probe` on the singleton so the UI shows a fresh check."""
    await hydrate_env_from_mongo(db)
    creds_present = bool(
        (os.environ.get("WEBULL_APP_KEY") or "").strip()
        and (os.environ.get("WEBULL_APP_SECRET") or "").strip()
        and (os.environ.get("WEBULL_ACCOUNT_ID") or "").strip()
    )
    if not creds_present:
        raise HTTPException(
            status_code=404,
            detail="No Webull credentials configured. Use POST /api/admin/webull/connect.",
        )

    # Lazy-import to avoid a circular dep and to keep the trader package
    # optional from a static-analysis standpoint.
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from trader import webull_auth as _wa  # noqa: WPS433
        token_status = _wa.status()
    except Exception as e:  # noqa: BLE001
        token_status = {"present": False, "error": f"{type(e).__name__}: {e}"}

    now = _now_iso()
    # 2026-07-21: the local token status is UNRELIABLE — it inferred
    # NORMAL from the deleted sidecar's spread poller and can never
    # flip anymore. The authoritative check is a LIVE trade-API call:
    # if open-orders answers, trading is active. The operator did the
    # 2FA dance three times while the UI read the dead flag.
    live: dict = {"ok": False, "checked": False}
    try:
        from shared.broker.webull import get_webull_adapter  # noqa: WPS433
        adapter = await get_webull_adapter()
        if adapter is None:
            live = {"ok": False, "checked": True, "error": "adapter_not_configured"}
        else:
            rows = await adapter.list_open_orders_v3(page_size=10)
            live = {"ok": True, "checked": True, "open_orders_seen": len(rows)}
    except Exception as e:  # noqa: BLE001
        live = {
            "ok": False, "checked": True,
            "error": f"{type(e).__name__}: {str(e)[:180]}",
        }

    ok = live["ok"]
    detail = {
        "endpoint": "webull trade API (list_open_orders_v3)",
        "live_trade_api": live,
        "token_present": token_status.get("present", False),
        "token_expired": token_status.get("expired", False),
        "token_expires_in_hours": token_status.get("expires_in_hours"),
        "token_preview": token_status.get("preview"),
    }
    if not ok:
        detail["hint"] = (
            "Live trade-API call failed — see live_trade_api.error. "
            "If it mentions auth/token, trigger the 2FA push and enter "
            "the SMS code inside the Webull app, then probe again."
        )

    await db[WEBULL_CREDENTIALS].update_one(
        {"_id": "singleton"},
        {"$set": {"last_probe": {"ok": ok, "ts": now, **detail}, "updated_at": now}},
    )
    return {"ok": ok, "ts": now, **detail}


@router.delete("/disconnect")
async def disconnect(user: dict = Depends(get_current_user)):
    """Wipe the singleton and clear the in-process env. Idempotent."""
    result = await db[WEBULL_CREDENTIALS].delete_one({"_id": "singleton"})
    clear_env()
    await _audit(
        "webull_disconnect",
        user.get("email") or "operator",
        {"deleted": result.deleted_count},
    )
    return {"ok": True, "deleted": result.deleted_count}


# ═══════════════════════════════════════════════════════════════════
# Re-authorize — one-click hot path (2026-02-19 operator directive)
# ═══════════════════════════════════════════════════════════════════
#
# Doctrine: the Webull x-access-token expires every 15 days
# server-side. Prior workflow required an operator SSH into the pod
# and manual `curl` OR a redeploy to reset the token. That's wrong;
# the operator needs a UI button that re-triggers the 2FA push flow
# on demand.
#
# What this endpoint does:
#   1. Verify app_key/app_secret are configured (fail early with a
#      404 pointing at /connect if not).
#   2. Force-invalidate the in-process token cache so the next read
#      does NOT return a stale copy.
#   3. Optionally purge the disk file so `_read_from_disk` cannot
#      fall back to a stale local copy while the new token is
#      pending 2FA approval. Toggle via `purge_disk` param
#      (default True — safer).
#   4. Trigger `webull_auth.create_token()`, which POSTs to
#      `/openapi/auth/token/create`. Webull server sends a push
#      notification to the operator's mobile app. On approval,
#      status flips PENDING → NORMAL server-side and equity spreads
#      resume flowing.
#   5. Write the new token to disk AND Mongo mirror (create_token
#      already does this via `_write_to_disk`).
#   6. Return the sanitized payload + a message the UI can display.
#
# The new token is IMMEDIATELY usable across redeploys because the
# Mongo mirror survives and the read path now prefers whichever
# tier has the newer `created_at`.
class ReauthIn(BaseModel):
    purge_disk: bool = Field(
        default=True,
        description=(
            "Delete the local disk token file before triggering the "
            "new token push. Prevents a stale copy from being served "
            "while the new push is pending 2FA approval."
        ),
    )


@router.post("/reauth")
async def reauth(
    body: ReauthIn | None = None,
    user: dict = Depends(get_current_user),
):
    """Trigger a fresh Webull token push — no redeploy required.

    Prereq: `POST /api/admin/webull/connect` must have run at least
    once so app_key + app_secret are on file.

    On success returns HTTP 200 with `status="PENDING"`. Operator
    approves the mobile push, then Webull server flips server-side
    status to NORMAL within ~30s. Poll
    `GET /api/admin/trader/webull-token-status` to confirm.
    """
    body = body or ReauthIn()
    await hydrate_env_from_mongo(db)
    creds_present = bool(
        (os.environ.get("WEBULL_APP_KEY") or "").strip()
        and (os.environ.get("WEBULL_APP_SECRET") or "").strip()
    )
    if not creds_present:
        raise HTTPException(
            status_code=404,
            detail=(
                "No Webull app_key/app_secret configured. "
                "Use POST /api/admin/webull/connect first."
            ),
        )

    # Lazy-import so trader package failure doesn't crash the route
    # module at import time.
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    try:
        from trader import webull_auth as _wa  # noqa: WPS433
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"webull_auth import failed: {type(e).__name__}: {e}",
        )

    pre_status = None
    try:
        pre_status = _wa.status()
    except Exception:  # noqa: BLE001
        pass

    # Force cache invalidation BEFORE the push. If the push succeeds
    # we replace the cache with the fresh payload; if it fails,
    # the next read will re-hydrate from disk/Mongo as normal.
    with _wa._lock:  # noqa: SLF001 - deliberate cache-invalidation touch
        _wa._cache = None  # noqa: SLF001

    # Optionally purge disk. Keeps Mongo mirror intact — if this pod
    # crashes before the new token lands, next boot rehydrates from
    # Mongo (which still has the PREVIOUS good token).
    disk_purged = False
    if body.purge_disk:
        try:
            p = _wa._token_path()  # noqa: SLF001
            if p.exists():
                p.unlink()
                disk_purged = True
                logger.info("webull_reauth: purged stale disk token at %s", p)
        except Exception as e:  # noqa: BLE001
            logger.warning("webull_reauth: disk purge failed: %s", e)

    # Trigger the actual push.
    try:
        payload = await _wa.create_token()
    except RuntimeError as e:
        await _audit(
            "webull_reauth_failed",
            user.get("email") or "operator",
            {"error": str(e), "disk_purged": disk_purged},
        )
        raise HTTPException(status_code=502, detail=str(e))

    await _audit(
        "webull_reauth",
        user.get("email") or "operator",
        {
            "disk_purged": disk_purged,
            "pre_status": (pre_status or {}).get("reported_status"),
            "post_status": payload.get("status"),
            "expires": payload.get("expires"),
        },
    )

    return {
        "ok": True,
        "message": (
            "Push sent. Webull will SMS you a 6-digit code — enter it "
            "INSIDE the Webull mobile app (Menu → Messages → OpenAPI "
            "Notifications → Check Now → enter code → Confirm). Once "
            "server-side status flips to NORMAL (~30s after confirm), "
            "equity spreads and orders resume automatically — no "
            "redeploy required."
        ),
        "disk_purged": disk_purged,
        "mongo_mirror_updated": True,
        **payload,
        "checked_at": _now_iso(),
    }


def _public_status(doc: dict) -> dict:
    """Shape the singleton doc for UI consumption. Never leaks the
    encrypted secret or unredacted account_id."""
    return {
        "connected": True,
        "app_key_preview": doc.get("app_key_preview"),
        "app_secret_preview": doc.get("app_secret_preview"),
        "account_id_preview": doc.get("account_id_preview"),
        "region_id": doc.get("region_id"),
        "environment": doc.get("environment"),
        "last_probe": doc.get("last_probe") or {},
        "created_at": doc.get("created_at"),
        "updated_at": doc.get("updated_at"),
        "connected_by": doc.get("connected_by"),
        "cred_source": "mongo",
    }
