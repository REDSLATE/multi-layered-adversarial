"""Live broker executors — Kraken (crypto) + Webull (equity).

These are deliberately thin. They translate (asset, side, notional)
into a market order on the live broker and return the broker's
response dict. NO retries, NO caching, NO state. The trader's risk
layer is the only gate; the broker is the truth.

If the broker rejects, the executor raises BrokerError with the raw
broker response in `.detail`. The trader's `audit.py` captures that
into the executions row.

═════════════════════════════════════════════════════════════════════
 SHADOW-MODE DOCTRINE (2026-07-09 operator directive, iter-22)
═════════════════════════════════════════════════════════════════════
Production had two execution authorities (this sidecar + the MC
auto_router). Operator directive:

    "There is only one broker door. MC owns it.
     Sidecar cannot submit orders."

Both `kraken_market_order` and `webull_market_order` now consult
the `TRADER_ENABLED` env flag (default: false → shadow mode). When
shadow, they SHORT-CIRCUIT before the network call and return a
synthetic `{shadow_only: true, ...}` dict so the audit tape still
records the intent-would-have-fired signal for diagnostic value.
NO order ever leaves the box from this file when shadow is on.

Decommission plan (staged, per operator):
    1. Shadow the sidecar (TRADER_ENABLED=false)         ← this iter
    2. Confirm MC path fires/blocks correctly alone       ← observe
    3. Strip broker credentials from sidecar env
    4. Delete sidecar env vars + UI references
    5. Delete /app/trader after one stable cycle
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


logger = logging.getLogger("trader.broker")


class BrokerError(Exception):
    def __init__(self, msg: str, detail: dict | None = None):
        super().__init__(msg)
        self.detail = detail or {}


def _trader_enabled() -> bool:
    """Read the sidecar-authority env flag. Default FALSE (shadow).

    This is the ONLY place in the codebase that should ever gate a
    live broker submit from the sidecar. If you're adding a new
    broker executor here, ALSO plumb it through `_shadow_receipt`
    below — the operator's audit tape must keep receiving would-fire
    events even while the switch is dark."""
    raw = os.environ.get("TRADER_ENABLED", "false").lower().strip()
    return raw in {"1", "true", "yes", "on"}


def _shadow_receipt(broker: str, **kw) -> dict:
    """Return a synthetic broker response signalling that the intent
    was intercepted before hitting the wire. Downstream `audit.py`
    treats a `shadow_only=True` receipt as a diagnostic emission
    (never as a fill), and the `receipts.jsonl` history stays useful
    for post-mortem analysis."""
    logger.warning(
        "trader.broker SHADOWED (TRADER_ENABLED=false) broker=%s kw=%s",
        broker, {k: v for k, v in kw.items() if k != "secret"},
    )
    return {
        "shadow_only": True,
        "broker": broker,
        "reason": "TRADER_ENABLED=false — sidecar demoted to shadow "
                  "(MC auto_router is the sole broker door).",
        "shadowed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **kw,
    }


# ── Kraken (crypto) ──────────────────────────────────────────────
KRAKEN_BASE = "https://api.kraken.com"


def _kraken_sign(path: str, data: dict, secret: str) -> str:
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()
    sig = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(sig.digest()).decode()


async def kraken_market_order(
    *,
    pair: str,
    side: str,           # "buy" or "sell"
    volume: str,         # base-asset quantity, e.g. "0.001"
) -> dict:
    """Place a live Kraken market order. Returns the broker's full
    `result` dict on success (contains `txid`). Raises BrokerError
    on any Kraken-side rejection.

    Shadow-mode: when `TRADER_ENABLED` is falsy (the production
    default now), this function SHORT-CIRCUITS before the network
    call and returns a `{shadow_only: True, ...}` receipt. No order
    is ever sent to Kraken from the sidecar in shadow mode."""
    if not _trader_enabled():
        return _shadow_receipt(
            broker="kraken", pair=pair, side=side, volume=volume,
        )
    key = os.environ.get("KRAKEN_API_KEY")
    secret = os.environ.get("KRAKEN_API_SECRET")
    if not key or not secret:
        raise BrokerError("kraken credentials missing in env")
    path = "/0/private/AddOrder"
    nonce = str(int(time.time() * 1000))
    data = {
        "nonce": nonce,
        "ordertype": "market",
        "type": side.lower(),
        "volume": str(volume),
        "pair": pair,
    }
    headers = {
        "API-Key": key,
        "API-Sign": _kraken_sign(path, data, secret),
    }
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(KRAKEN_BASE + path, data=data, headers=headers)
        r.raise_for_status()
        j = r.json()
    if j.get("error"):
        raise BrokerError(f"kraken_rejected: {j['error']}", detail=j)
    return j.get("result") or {}


# ── Webull (equity) ──────────────────────────────────────────────
WEBULL_BASE = "https://u1strade.webullbroker.com/api/trade/v1"


async def webull_market_order(
    *,
    ticker: str,
    side: str,           # "BUY" or "SELL"
    notional_usd: float,
    last_price: float,
) -> dict:
    """Place a live Webull market order using the QTY entrust-type
    pattern (the only one that works for fractional shares on the
    v2 OpenAPI). Derives quantity = notional_usd / last_price.

    Raises BrokerError on any Webull-side rejection.

    Shadow-mode: when `TRADER_ENABLED` is falsy, SHORT-CIRCUITS
    before the network call. See module docstring."""
    if not _trader_enabled():
        return _shadow_receipt(
            broker="webull", ticker=ticker, side=side,
            notional_usd=notional_usd, last_price=last_price,
        )
    app_key = os.environ.get("WEBULL_APP_KEY")
    app_secret = os.environ.get("WEBULL_APP_SECRET")
    account_id = os.environ.get("WEBULL_ACCOUNT_ID")
    if not (app_key and app_secret and account_id):
        raise BrokerError("webull credentials missing in env")
    if not last_price or last_price <= 0:
        raise BrokerError(f"webull invalid last_price={last_price!r}")
    qty = round(notional_usd / last_price, 4)
    if qty <= 0:
        raise BrokerError(
            f"webull computed qty={qty} from notional={notional_usd}/price={last_price}"
        )
    payload = {
        "account_id": account_id,
        "ticker": ticker.upper(),
        "action": side.upper(),
        "order_type": "MKT",
        "time_in_force": "DAY",
        "entrust_type": "QTY",
        "quantity": str(qty),
        "account_tax_type": "GENERAL",
    }
    headers = {
        "X-APP-KEY": app_key,
        "X-APP-SECRET": app_secret,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(
            f"{WEBULL_BASE}/orders/place",
            json=payload, headers=headers,
        )
        body: dict = {}
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = {"raw": r.text[:500]}
        if r.status_code >= 300 or not body.get("success", True):
            raise BrokerError(
                f"webull_rejected status={r.status_code}",
                detail={"status": r.status_code, "body": body, "payload": payload},
            )
        return body
