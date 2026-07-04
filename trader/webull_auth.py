"""Webull OpenAPI access-token lifecycle.

Doctrine pin (2026-07-02):
    The market-data snapshot endpoint requires `x-access-token`, a
    32-hex credential generated via a 2FA flow:
        1. Client → POST /openapi/auth/token/create (signed).
        2. Webull → returns {token, expires, status=PENDING}.
        3. Webull → push notification to the operator's mobile app.
        4. Operator → approves in the Webull app.
        5. Webull → flips server-side status to NORMAL.
        6. Client → uses the token on subsequent signed requests.

Tokens are valid 15 days by default. We persist to a local JSON
file (same directory as the SQLite tape, so a future persistent
volume makes tokens durable across pod restarts) and expose a
cheap in-memory getter for `spread.py`.

No Mongo. No secrets logged. Never raises on I/O failures.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from trader import spread


logger = logging.getLogger("trader.webull_auth")

_TOKEN_PATH_ENV = "WEBULL_TOKEN_PATH"
_DEFAULT_TOKEN_FILENAME = "webull_token.json"
CREATE_PATH = "/openapi/auth/token/create"

_lock = threading.Lock()
_cache: Optional[dict] = None


def _token_path() -> Path:
    override = os.environ.get(_TOKEN_PATH_ENV)
    if override:
        return Path(override)
    # Sit next to the SQLite tape so a future PV covers both.
    from trader import config as _config
    return Path(_config.jsonl_dir()) / _DEFAULT_TOKEN_FILENAME


# ── Mongo mirror (2026-07-04, operator directive P1a) ────────────────
# The disk path (`/app/trader/data/webull_token.json`) is EPHEMERAL —
# it lives on the pod's writable overlay and gets wiped on every
# redeploy. That means every deploy forced the operator to re-run
# the 2FA push flow to reissue a token, which made it impossible to
# leave live-money trading enabled across deploys.
#
# Fix: mirror the token payload to a MongoDB singleton collection
# (`webull_token`, doc `_id="current"`). MongoDB is external-managed
# and survives redeploys. On startup, if the disk file is missing
# but Mongo has a payload, rehydrate disk from Mongo transparently.
#
# One-time 2FA cost per token TTL (15 days server-side) instead of
# per-deploy. Sync `pymongo` used deliberately — reads/writes happen
# once per 15-day cycle, no perf concern, and this avoids threading
# an async-motor handle through sync callers (`_read_from_disk` is
# called from sync `get_token()` on every quote fetch).
_MONGO_COLL_NAME = "webull_token"
_MONGO_DOC_ID = "current"


def _mongo_collection():
    """Return the pymongo sync collection handle, or None if the
    environment isn't configured. Never raises."""
    try:
        import pymongo  # noqa: WPS433
    except ImportError:
        return None
    url = os.environ.get("MONGO_URL")
    name = os.environ.get("DB_NAME")
    if not url or not name:
        return None
    try:
        client = pymongo.MongoClient(url, serverSelectionTimeoutMS=3000)
        return client[name][_MONGO_COLL_NAME]
    except Exception as e:  # noqa: BLE001
        logger.warning("webull_token mongo handle failed: %s", e)
        return None


def _read_from_mongo() -> Optional[dict]:
    coll = _mongo_collection()
    if coll is None:
        return None
    try:
        doc = coll.find_one({"_id": _MONGO_DOC_ID})
        if not doc:
            return None
        doc.pop("_id", None)
        return doc
    except Exception as e:  # noqa: BLE001
        logger.warning("webull_token mongo read failed: %s", e)
        return None


def _write_to_mongo(payload: dict) -> None:
    coll = _mongo_collection()
    if coll is None:
        return
    try:
        coll.replace_one(
            {"_id": _MONGO_DOC_ID},
            {**payload, "_id": _MONGO_DOC_ID},
            upsert=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("webull_token mongo write failed: %s", e)


def _read_from_disk() -> Optional[dict]:
    p = _token_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception as e:  # noqa: BLE001
            logger.warning("webull_token read failed path=%s err=%s", p, e)
            # fall through to mongo restore

    # Disk empty (fresh pod / post-redeploy) — try Mongo mirror.
    mongo_payload = _read_from_mongo()
    if mongo_payload:
        logger.info(
            "webull_token restored from Mongo mirror path=%s "
            "(disk was empty — likely post-redeploy)", p,
        )
        # Rehydrate disk for subsequent fast reads.
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(mongo_payload, indent=2))
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "webull_token disk rehydrate failed path=%s err=%s", p, e,
            )
        return mongo_payload
    return None


def _write_to_disk(payload: dict) -> None:
    p = _token_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, indent=2))
    except Exception as e:  # noqa: BLE001
        logger.warning("webull_token write failed path=%s err=%s", p, e)
    # Mirror to Mongo for redeploy persistence. Best-effort — if
    # Mongo is unreachable, disk write still succeeded and the
    # current pod keeps working; next redeploy would lose the token
    # but that's the pre-fix behavior, not a regression.
    _write_to_mongo(payload)


def get_token() -> Optional[str]:
    """Return the current access token from cache/disk, or None.
    `spread._webull_creds()` calls this first, then falls back to
    the WEBULL_ACCESS_TOKEN env var.

    We deliberately DO NOT enforce the local `expires` value here —
    it's the pre-approval TTL from Webull's create response (usually
    ~6 min). Once the operator approves via 2FA, Webull server-side
    extends validity to 15 days but never tells us the new expiry.
    So we let Webull authoritatively reject the token if it's stale
    (401 UNAUTHORIZED, surfaced in the log for operator awareness)
    rather than lock ourselves out with an outdated local guess.
    """
    global _cache
    with _lock:
        if _cache is None:
            _cache = _read_from_disk()
        if not _cache:
            return None
        return _cache.get("token") or None


def _sanitized(payload: dict) -> dict:
    """Return a copy of the token payload with the token itself
    truncated — safe for the UI response and logs."""
    out = dict(payload)
    tok = out.get("token") or ""
    if tok:
        out["token_preview"] = f"{tok[:6]}…{tok[-4:]}"
        out["token_length"] = len(tok)
        # Never surface the full token over HTTP or logs.
        out.pop("token", None)
    return out


async def create_token() -> dict:
    """Trigger the create-token flow. Returns the sanitized payload
    the API/UI should echo back. Persists the token to disk on
    success. Raises `RuntimeError` on any Webull-side failure so
    the admin endpoint can turn it into a clear 5xx for the operator.
    """
    creds = spread._webull_creds()  # (key, secret, existing_token_or_"")
    if not creds:
        raise RuntimeError(
            "WEBULL_APP_KEY / WEBULL_APP_SECRET are not set in backend/.env"
        )
    app_key, app_secret, _ = creds
    base = spread._webull_openapi_base()
    url = base + CREATE_PATH
    host = base.split("://", 1)[-1].split("/", 1)[0]
    # POST with empty body — no body_string in the signature.
    headers = spread._webull_headers(
        app_key=app_key,
        app_secret=app_secret,
        access_token="",   # not required for token/create
        method="POST",
        path=CREATE_PATH,
        host=host,
        query=None,
        body="",
    )
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.post(url, headers=headers, content=b"")
            body = r.text
            try:
                j = r.json()
            except Exception:  # noqa: BLE001
                j = None
            if r.status_code != 200 or not isinstance(j, dict):
                raise RuntimeError(
                    f"Webull HTTP {r.status_code}: {body[:200]}"
                )
        except httpx.HTTPError as e:
            raise RuntimeError(f"Webull network error: {e}") from e
    tok = j.get("token")
    if not tok:
        raise RuntimeError(f"Webull returned no token: {body[:200]}")
    payload = {
        "token": tok,
        "expires": j.get("expires"),
        "status": j.get("status") or "PENDING",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base": base,
    }
    with _lock:
        global _cache
        _cache = payload
        _write_to_disk(payload)
    logger.info(
        "webull_token created status=%s expires=%s",
        payload["status"], payload["expires"],
    )
    return _sanitized(payload)


def status() -> dict:
    """Cheap read for the UI — never hits Webull."""
    with _lock:
        global _cache
        if _cache is None:
            _cache = _read_from_disk()
    if not _cache:
        return {"present": False, "source": "none"}
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    exp = _cache.get("expires") or 0
    # Webull's `POST /openapi/auth/token/create` response always says
    # PENDING with a ~6-minute TTL; once the operator approves via
    # 2FA the server flips it to NORMAL server-side and extends the
    # expiry to 15 days. We infer "NORMAL" locally by watching the
    # spread poller's success — if the equity poller has cached a
    # tick after our token was created, the token must be active.
    reported = _cache.get("status")
    effective = reported
    if reported == "PENDING":
        try:
            from trader import spread as _spread  # noqa: WPS433
            created_ts = _cache.get("created_at")
            if created_ts:
                created = datetime.fromisoformat(created_ts).timestamp()
            else:
                created = 0
            for row in _spread.latest() or []:
                if row.get("source") != "webull":
                    continue
                row_ts = row.get("ts_unix") or 0
                if row_ts > created:
                    effective = "NORMAL"
                    break
        except Exception:  # noqa: BLE001
            pass
    return {
        "present": True,
        "source": "disk",
        "status": effective,
        "reported_status": reported,
        "expires": exp,
        "expired": bool(exp) and exp < now_ms,
        "expires_in_hours": (
            round((exp - now_ms) / 3_600_000, 1) if exp else None
        ),
        **_sanitized(_cache),
    }
