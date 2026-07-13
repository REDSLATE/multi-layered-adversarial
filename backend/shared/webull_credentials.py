"""Webull credential source-of-truth resolver (2026-02-17).

Companion to `shared.crypto.kraken.get_active_keys()` — same pattern
(env-first for backward compat, Mongo singleton fallback), reshaped
for Webull's three-value credential tuple (app_key, app_secret,
account_id) plus its two axes of environment (region_id ∈ {us,hk,jp}
and environment ∈ {pro,paper}).

Note: Webull's own API has a `paper` environment option distinct from
`pro`. Per the 2026-02-19 operator directive (LIVE ONLY), MC always
selects `pro` at connect-time; `paper` is retained in the enum here
purely because Webull's API rejects requests that omit it. The stored
credential singleton MUST have `environment="pro"` for any order to
route.

Two callers exist:
    1. Async FastAPI routes → `get_active_webull_creds()` (motor).
    2. Sync trader threads (`trader/spread.py`, `trader/broker.py`,
       `trader/webull_auth.py`) → they read via `os.environ` today.
       To keep those callers untouched we mirror Mongo values BACK
       INTO the running process env at connect-time via
       `hydrate_env_from_mongo()` — called (a) at server startup from
       lifespan, (b) at the end of `POST /api/admin/webull/connect`
       right after the write.

Doctrine (mirrors Kraken):
    - The plaintext app_secret only exists in memory when it's used.
    - The Fernet key never leaves the backend process.
    - The API never round-trips ciphertext — only redacted previews
      surface to the UI.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from shared.credentials import decrypt


logger = logging.getLogger("shared.webull_credentials")

# Public because tests + probe route both read it.
COLLECTION = "webull_credentials"
DOC_ID = "singleton"


async def get_active_webull_creds(db) -> Optional[dict]:
    """Return `{app_key, app_secret, account_id, region_id, environment,
    source}` or None. Env vars take precedence for backward compat;
    Mongo singleton is the fallback.

    `source` ∈ {"env", "mongo", "none"} — matches the Kraken resolver
    contract.
    """
    env_key = (os.environ.get("WEBULL_APP_KEY") or "").strip().strip('"').strip("'")
    env_secret = (os.environ.get("WEBULL_APP_SECRET") or "").strip().strip('"').strip("'")
    env_account = (os.environ.get("WEBULL_ACCOUNT_ID") or "").strip().strip('"').strip("'")
    if env_key and env_secret and env_account:
        return {
            "app_key": env_key,
            "app_secret": env_secret,
            "account_id": env_account,
            "region_id": (os.environ.get("WEBULL_REGION_ID") or "us").strip().strip('"').strip("'"),
            "environment": (os.environ.get("WEBULL_ENVIRONMENT") or "pro").strip().strip('"').strip("'"),
            "source": "env",
        }

    try:
        doc = await db[COLLECTION].find_one({"_id": DOC_ID}, {"_id": 0})
    except Exception as e:  # noqa: BLE001
        logger.warning("webull mongo cred lookup failed: %s", e)
        return None

    if not doc or not doc.get("app_key") or not doc.get("encrypted_app_secret"):
        return None

    try:
        app_secret = decrypt(doc["encrypted_app_secret"])
    except Exception as e:  # noqa: BLE001
        logger.error("webull encrypted_app_secret decrypt failed: %s", e)
        return None

    return {
        "app_key": doc["app_key"],
        "app_secret": app_secret,
        "account_id": doc.get("account_id", ""),
        "region_id": doc.get("region_id", "us"),
        "environment": doc.get("environment", "pro"),
        "source": "mongo",
    }


def hydrate_env_from_mongo_sync() -> bool:
    """Sync helper — usable from server startup where the async db
    handle isn't ergonomic. Uses `pymongo` off `MONGO_URL` + `DB_NAME`
    which are already set in `.env`. Never raises.

    Returns True if the process env was updated from Mongo; False if
    env was already set or Mongo had no doc / errored.
    """
    if (os.environ.get("WEBULL_APP_KEY") or "").strip():
        return False  # env already wins per doctrine

    try:
        import pymongo  # noqa: WPS433
    except ImportError:
        return False

    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    if not (mongo_url and db_name):
        return False

    try:
        client = pymongo.MongoClient(mongo_url, serverSelectionTimeoutMS=2000)
        doc = client[db_name][COLLECTION].find_one({"_id": DOC_ID})
        client.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("webull env hydration: mongo unreachable: %s", e)
        return False

    if not doc or not doc.get("app_key") or not doc.get("encrypted_app_secret"):
        return False

    try:
        app_secret = decrypt(doc["encrypted_app_secret"])
    except Exception as e:  # noqa: BLE001
        logger.error("webull env hydration: decrypt failed: %s", e)
        return False

    os.environ["WEBULL_APP_KEY"] = doc["app_key"]
    os.environ["WEBULL_APP_SECRET"] = app_secret
    if doc.get("account_id"):
        os.environ["WEBULL_ACCOUNT_ID"] = doc["account_id"]
    if doc.get("region_id"):
        os.environ["WEBULL_REGION_ID"] = doc["region_id"]
    if doc.get("environment"):
        os.environ["WEBULL_ENVIRONMENT"] = doc["environment"]

    logger.info("webull creds hydrated from mongo singleton into process env")
    return True


async def hydrate_env_from_mongo(db) -> bool:
    """Async variant of `hydrate_env_from_mongo_sync` — used inside a
    request handler where the motor `db` is already available."""
    if (os.environ.get("WEBULL_APP_KEY") or "").strip():
        return False

    try:
        doc = await db[COLLECTION].find_one({"_id": DOC_ID})
    except Exception as e:  # noqa: BLE001
        logger.warning("webull env hydration (async): mongo error: %s", e)
        return False

    if not doc or not doc.get("app_key") or not doc.get("encrypted_app_secret"):
        return False

    try:
        app_secret = decrypt(doc["encrypted_app_secret"])
    except Exception as e:  # noqa: BLE001
        logger.error("webull env hydration (async): decrypt failed: %s", e)
        return False

    os.environ["WEBULL_APP_KEY"] = doc["app_key"]
    os.environ["WEBULL_APP_SECRET"] = app_secret
    if doc.get("account_id"):
        os.environ["WEBULL_ACCOUNT_ID"] = doc["account_id"]
    if doc.get("region_id"):
        os.environ["WEBULL_REGION_ID"] = doc["region_id"]
    if doc.get("environment"):
        os.environ["WEBULL_ENVIRONMENT"] = doc["environment"]
    return True


def push_creds_into_env(app_key: str, app_secret: str, account_id: str,
                       region_id: str = "us", environment: str = "pro") -> None:
    """Overwrite in-process env after a successful connect. Sync
    callers in the trader threads read env fresh on each call, so this
    takes effect on the next quote/order cycle without restart."""
    os.environ["WEBULL_APP_KEY"] = app_key
    os.environ["WEBULL_APP_SECRET"] = app_secret
    os.environ["WEBULL_ACCOUNT_ID"] = account_id
    os.environ["WEBULL_REGION_ID"] = region_id
    os.environ["WEBULL_ENVIRONMENT"] = environment


def clear_env() -> None:
    """Remove all Webull cred env vars — called from DELETE /disconnect
    so the trader threads stop authenticating with revoked keys on the
    next tick. Idempotent."""
    for k in (
        "WEBULL_APP_KEY", "WEBULL_APP_SECRET", "WEBULL_ACCOUNT_ID",
        "WEBULL_REGION_ID", "WEBULL_ENVIRONMENT",
    ):
        os.environ.pop(k, None)


__all__ = [
    "COLLECTION",
    "DOC_ID",
    "get_active_webull_creds",
    "hydrate_env_from_mongo",
    "hydrate_env_from_mongo_sync",
    "push_creds_into_env",
    "clear_env",
]
