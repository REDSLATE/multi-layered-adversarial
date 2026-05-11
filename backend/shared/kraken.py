"""Kraken Pro REST API client.

Public OHLCV endpoint is unauthenticated. Private endpoints sign with
HMAC-SHA512 over `path + sha256(nonce + post_data)`, with the HMAC key
being the base64-decoded private key. See:
  https://support.kraken.com/articles/360029054811

Doctrine:
    - This client NEVER calls trading endpoints. It only reads.
    - The execution path (AddOrder / CancelOrder) is intentionally
      omitted. When you eventually wire it, it must go behind a separate
      gate that defaults off and audit-logs every flip.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import httpx


KRAKEN_BASE = "https://api.kraken.com"
USER_AGENT = "risedual-mission-control/1.0"

# Tier-2 default: 20-counter budget, refills 0.5/s. Conservative cap so we
# stay well clear of the limit when scope-probing five endpoints back-to-back.
_RATE_LIMIT_SEMAPHORE = asyncio.Semaphore(3)


# ──────────────────────── public endpoints ────────────────────────

async def fetch_ohlc(pair: str, interval_minutes: int = 60, since: int | None = None) -> dict:
    """Fetch OHLC bars from Kraken's public endpoint.

    `pair` accepts Kraken's altname (XBTUSD, ETHUSD, ...) or the more
    common dual-symbol form (BTC/USD, ETH/USD). Kraken normalises both.

    Returns the raw Kraken result dict. Caller is responsible for shaping
    bars into the internal OHLCVBarIn format.
    """
    params: dict[str, Any] = {"pair": pair, "interval": interval_minutes}
    if since is not None:
        params["since"] = since
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{KRAKEN_BASE}/0/public/OHLC",
            params=params,
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken public OHLC error: {data['error']}")
    return data.get("result", {})


# ──────────────────────── signing ────────────────────────

def _sign(path: str, nonce: str, post_body: str, private_key_b64: str) -> str:
    """Return the API-Sign header value for a private POST request.

    Algorithm:
        sha = sha256(nonce + post_body)
        sig = HMAC-SHA512(path.bytes + sha, base64decode(private_key))
        return base64(sig)
    """
    private_key = base64.b64decode(private_key_b64)
    sha = hashlib.sha256((nonce + post_body).encode("utf-8")).digest()
    mac = hmac.new(private_key, path.encode("utf-8") + sha, hashlib.sha512).digest()
    return base64.b64encode(mac).decode("utf-8")


async def _next_nonce() -> str:
    """Monotonically increasing nonce, persisted across restarts.

    Kraken requires each nonce to be > the previous one for the same key.
    We use ms-precision wall clock and bump on collision/regression.
    """
    from db import db
    from namespaces import KRAKEN_CREDENTIALS
    now_ms = int(time.time_ns() // 1_000_000)
    # Atomic max-update — bumps last_nonce to whichever is greater.
    doc = await db[KRAKEN_CREDENTIALS].find_one_and_update(
        {"_id": "singleton"},
        [
            {"$set": {"last_nonce": {"$max": ["$last_nonce", now_ms]}}},
            {"$set": {"last_nonce": {"$add": ["$last_nonce", 1]}}},
        ],
        upsert=False,
        return_document=True,
    )
    if not doc:
        # First-call edge case before the doc exists. Should not occur in
        # practice because every private call goes after credential save.
        return str(now_ms)
    return str(doc["last_nonce"])


# ──────────────────────── private endpoints ────────────────────────

class KrakenError(Exception):
    def __init__(self, errors: list[str], status: int = 200):
        super().__init__(", ".join(errors) if errors else "unknown Kraken error")
        self.errors = errors
        self.status = status


async def call_private(
    path: str,
    public_key: str,
    private_key_b64: str,
    params: dict[str, Any] | None = None,
) -> dict:
    """Sign + POST to a private endpoint, returning the parsed `result` dict.

    Raises KrakenError on API-level error (Kraken's `error` array
    populated). Re-raises httpx errors on transport issues.
    """
    if params is None:
        params = {}
    async with _RATE_LIMIT_SEMAPHORE:
        nonce = await _next_nonce()
        body = {"nonce": nonce, **params}
        post_body = urllib.parse.urlencode(body)
        sig = _sign(path, nonce, post_body, private_key_b64)
        headers = {
            "API-Key": public_key,
            "API-Sign": sig,
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{KRAKEN_BASE}{path}", content=post_body, headers=headers,
            )
        # Kraken usually returns 200 with `error` populated; non-200 is
        # transport / DDoS layer.
        try:
            data = r.json()
        except Exception as e:  # noqa: BLE001
            raise KrakenError([f"non-JSON response: {r.text[:120]}"], status=r.status_code) from e
        errs = data.get("error") or []
        if errs:
            raise KrakenError(errs, status=r.status_code)
        return data.get("result", {})


# ──────────────────────── scope probe ────────────────────────

# Five endpoints, each gating a different permission Kraken exposes on
# its key-permissions screen. Cheap GET-shaped calls so the probe runs
# fast (<2s end-to-end with the semaphore in front).
SCOPE_PROBES = [
    ("query_funds",        "/0/private/Balance",       {}),
    ("query_open_positions","/0/private/OpenPositions",{}),
    ("query_closed_orders","/0/private/ClosedOrders",  {}),
    ("query_trades",       "/0/private/TradesHistory", {}),
    ("query_ledger",       "/0/private/Ledgers",       {}),
]


import logging

logger = logging.getLogger("risedual.kraken")


async def probe_scopes(public_key: str, private_key_b64: str) -> dict[str, bool | str]:
    """Probe the five common read scopes.

    Returns a dict keyed by scope name with bool values. The probe also
    returns:
      - `_balance_preview`: a tiny preview of the Balance call so the UI
        can confirm the keys are alive without us having to re-call.
      - `_errors`: per-scope error message if the probe failed.
    """
    out: dict[str, bool | str | dict] = {}
    errors: dict[str, str] = {}
    balance_preview = None
    for scope, path, params in SCOPE_PROBES:
        try:
            result = await call_private(path, public_key, private_key_b64, params)
            out[scope] = True
            if scope == "query_funds":
                balance_preview = _summarise_balance(result)
        except KrakenError as e:
            # "Invalid permissions" is the expected denial; everything
            # else is reported but treated as "scope unavailable".
            msg = "; ".join(e.errors)
            out[scope] = False
            errors[scope] = msg
            logger.warning("Kraken probe %s: %s", scope, msg)
        except httpx.HTTPError as e:
            out[scope] = False
            errors[scope] = f"transport: {e}"
            logger.warning("Kraken probe %s transport error: %s", scope, e)
        # Tiny gap to be friendly with the rate limit.
        await asyncio.sleep(0.15)
    out["_balance_preview"] = balance_preview
    out["_errors"] = errors
    return out


def _summarise_balance(balance_result: dict) -> dict:
    """Reduce the Balance response to a tiny preview safe for UI display."""
    if not isinstance(balance_result, dict):
        return {}
    # Show top-3 assets by absolute float value.
    items: list[tuple[str, float]] = []
    for asset, qty in balance_result.items():
        try:
            items.append((asset, float(qty)))
        except (TypeError, ValueError):
            continue
    items.sort(key=lambda kv: abs(kv[1]), reverse=True)
    return {asset: f"{qty:.8f}".rstrip("0").rstrip(".") for asset, qty in items[:3]}


# ──────────────────────── credential resolution ────────────────────────

async def get_active_keys() -> tuple[str, str] | None:
    """Decrypt and return (public_key, private_key) for the singleton
    credential record, or None if none configured."""
    from db import db
    from namespaces import KRAKEN_CREDENTIALS
    from shared.credentials import decrypt
    doc = await db[KRAKEN_CREDENTIALS].find_one({"_id": "singleton"}, {"_id": 0})
    if not doc or not doc.get("encrypted_private_key"):
        return None
    try:
        priv = decrypt(doc["encrypted_private_key"])
    except ValueError:
        return None
    return doc["public_key"], priv


# ──────────────────────── symbol mapping ────────────────────────

# Map our internal symbol to Kraken's REST pair name. Kraken's REST OHLC
# endpoint accepts both XBTUSD and BTC/USD but returns results keyed by
# its canonical altname (XXBTZUSD, XETHZUSD, ...). We keep this table
# tight; expand as you add pairs.
INTERNAL_TO_KRAKEN_PAIR = {
    "BTC/USD": "XBTUSD",
    "ETH/USD": "ETHUSD",
    "SOL/USD": "SOLUSD",
    "XRP/USD": "XRPUSD",
    "ADA/USD": "ADAUSD",
    "DOGE/USD": "DOGEUSD",
}


def to_kraken_pair(symbol: str) -> str:
    return INTERNAL_TO_KRAKEN_PAIR.get(symbol.upper(), symbol.upper().replace("/", ""))


def kraken_interval_for_tf(tf: str) -> int:
    """Map our internal tf to Kraken's minute-based interval."""
    return {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}[tf]


def to_internal_bar(symbol: str, tf: str, row: list) -> dict:
    """Kraken OHLC row: [time, open, high, low, close, vwap, volume, count].
    Returns our internal OHLCVBarIn shape (without `source`)."""
    ts = int(row[0])
    return {
        "symbol": symbol,
        "tf": tf,
        "ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
        "o": float(row[1]),
        "h": float(row[2]),
        "l": float(row[3]),
        "c": float(row[4]),
        "v": float(row[6]),
    }
