"""Broker connectivity probe.

Answers: "Can the trader actually reach Kraken and Webull with the
credentials we have RIGHT NOW?" Uses lightweight, READ-ONLY calls
(Balance / account query) so probing a broker never creates an
order and never spends any of the daily cap.

Credential resolution mirrors what the trader actually does at
runtime:

    Kraken:
        1. env `KRAKEN_API_KEY` + `KRAKEN_API_SECRET`
        2. Mongo `kraken_credentials.singleton` (encrypted via
           `shared.credentials.decrypt`)
        3. nothing → `connected=False, cred_source="none"`

    Webull:
        1. env `WEBULL_APP_KEY` + `WEBULL_APP_SECRET` + `WEBULL_ACCOUNT_ID`
        2. nothing → `connected=False, cred_source="none"`

All network calls are bounded by an 8-second timeout. This probe is
safe to run on demand from the operator dashboard.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from typing import Optional

import httpx


logger = logging.getLogger("trader.broker_check")

_KRAKEN_BASE = "https://api.kraken.com"
_WEBULL_BASE = "https://u1strade.webullbroker.com/api/trade/v1"
_TIMEOUT = 8.0


def _kraken_sign(path: str, data: dict, secret: str) -> str:
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()
    sig = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(sig.digest()).decode()


async def _kraken_credentials(db) -> tuple[Optional[str], Optional[str], str]:
    """Return `(key, secret, source)`. source ∈ {'env', 'mongo', 'none'}."""
    env_key = os.environ.get("KRAKEN_API_KEY")
    env_secret = os.environ.get("KRAKEN_API_SECRET")
    if env_key and env_secret:
        return env_key, env_secret, "env"
    try:
        # Lazy import — keeps this module usable in isolation for tests.
        from shared.crypto.kraken import get_active_keys  # noqa: WPS433
        pair = await get_active_keys()
        if pair:
            pub, priv = pair
            if pub and priv:
                return pub, priv, "mongo"
    except Exception as e:  # noqa: BLE001
        logger.warning("kraken mongo cred resolve failed: %s", e)
    return None, None, "none"


async def probe_kraken(db) -> dict:
    """Kraken `Balance` call — read-only, proves auth works."""
    key, secret, source = await _kraken_credentials(db)
    if source == "none":
        return {
            "connected": False,
            "cred_source": "none",
            "error": "no KRAKEN_API_KEY/SECRET in env and no encrypted keys in mongo",
            "hint": (
                "Either set env vars KRAKEN_API_KEY + KRAKEN_API_SECRET "
                "and redeploy, or POST /api/admin/kraken/connect with your keys."
            ),
        }
    path = "/0/private/Balance"
    nonce = str(int(time.time() * 1000))
    data = {"nonce": nonce}
    try:
        headers = {
            "API-Key": key,
            "API-Sign": _kraken_sign(path, data, secret),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "connected": False,
            "cred_source": source,
            "error": f"kraken_sign_failed: {e}",
            "hint": "The API secret is likely malformed base64; regenerate on Kraken.",
        }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(_KRAKEN_BASE + path, data=data, headers=headers)
    except httpx.TimeoutException:
        return {"connected": False, "cred_source": source, "error": "timeout"}
    except Exception as e:  # noqa: BLE001
        return {"connected": False, "cred_source": source,
                "error": f"http_error: {type(e).__name__}: {e}"}
    try:
        j = r.json()
    except Exception:  # noqa: BLE001
        j = {"raw": r.text[:200]}
    if r.status_code >= 300:
        return {
            "connected": False,
            "cred_source": source,
            "error": f"kraken_http_{r.status_code}",
            "body_preview": j,
        }
    if j.get("error"):
        return {
            "connected": False,
            "cred_source": source,
            "error": f"kraken_rejected: {j['error']}",
            "hint": (
                "Common causes: EAPI:Invalid key (typo), "
                "EGeneral:Permission denied (missing 'Query Funds' permission on the key), "
                "EAPI:Invalid nonce (clock drift)."
            ),
        }
    balance = j.get("result") or {}
    return {
        "connected": True,
        "cred_source": source,
        "probe": {
            "endpoint": "Balance",
            "asset_count": len(balance),
            "sample_assets": list(balance.keys())[:5],
        },
    }


async def probe_webull() -> dict:
    """Webull account info call — read-only. Confirms auth + account
    exists on this deploy."""
    app_key = os.environ.get("WEBULL_APP_KEY")
    app_secret = os.environ.get("WEBULL_APP_SECRET")
    account_id = os.environ.get("WEBULL_ACCOUNT_ID")
    if not (app_key and app_secret and account_id):
        missing = [
            k for k, v in (
                ("WEBULL_APP_KEY", app_key),
                ("WEBULL_APP_SECRET", app_secret),
                ("WEBULL_ACCOUNT_ID", account_id),
            ) if not v
        ]
        return {
            "connected": False,
            "cred_source": "none",
            "error": f"missing env: {', '.join(missing)}",
            "hint": "Set these in backend/.env and redeploy.",
        }
    headers = {
        "X-APP-KEY": app_key,
        "X-APP-SECRET": app_secret,
    }
    url = f"{_WEBULL_BASE}/accounts/{account_id}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(url, headers=headers)
    except httpx.TimeoutException:
        return {"connected": False, "cred_source": "env", "error": "timeout"}
    except Exception as e:  # noqa: BLE001
        return {"connected": False, "cred_source": "env",
                "error": f"http_error: {type(e).__name__}: {e}"}
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {"raw": r.text[:200]}
    if r.status_code >= 300:
        return {
            "connected": False,
            "cred_source": "env",
            "error": f"webull_http_{r.status_code}",
            "body_preview": body,
            "hint": (
                "Common causes: bad APP_KEY/SECRET (400/401), "
                "wrong ACCOUNT_ID (404), account not enabled for OpenAPI (403)."
            ),
        }
    return {
        "connected": True,
        "cred_source": "env",
        "probe": {
            "endpoint": f"accounts/{account_id[:8]}…",
            "keys": list((body or {}).keys())[:8],
        },
    }
