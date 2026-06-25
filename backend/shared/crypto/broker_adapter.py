"""Kraken Pro live-trading broker adapter.

Doctrine:
    * LIVE crypto trading. Kraken does not offer paper.
    * Adapter NEVER decides identity. Caller passes a broker-native
      pair string (e.g. "XBTUSD") that the resolver translated FROM a
      canonical AssetKey (e.g. "CRYPTO:BTC-USD").
    * Order shape: market order, USD notional via Kraken's `ordertype:
      market` + `volume` (base units). We compute volume from notional
      using a fresh tick from the public ticker — same approach as
      Alpaca's notional orders, except Kraken doesn't accept notional
      directly so we size locally.
    * Day-1 caps live OUTSIDE this adapter (in exposure_caps_crypto.py).
      The adapter trusts the caller.

This adapter sits behind the same `submit_market_order` interface as the
Alpaca adapter so the broker router can call them uniformly.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx

from shared.crypto.kraken import (
    KRAKEN_BASE,
    USER_AGENT,
    KrakenError,
    call_private,
    get_active_keys,
    to_kraken_pair,
)


logger = logging.getLogger("risedual.broker.kraken")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _ticker_price(pair: str) -> float:
    """Fetch the latest mid price for a Kraken pair via the public ticker.
    Used to convert USD notional → base-asset volume at order-build time."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{KRAKEN_BASE}/0/public/Ticker",
            params={"pair": pair},
            headers={"User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        data = r.json()
    if data.get("error"):
        raise KrakenError(data["error"])
    result = data.get("result") or {}
    if not result:
        raise KrakenError([f"empty ticker for {pair}"])
    # Kraken returns one key per pair; we grab the first.
    _, payload = next(iter(result.items()))
    # `c` = last trade [price, lot volume]; use it as our mid proxy.
    last = float(payload["c"][0])
    if last <= 0:
        raise KrakenError([f"non-positive last price for {pair}: {last}"])
    return last


class KrakenLiveAdapter:
    """LIVE Kraken Pro trading adapter. Real money."""

    name = "kraken"
    is_paper = False

    def __init__(self, public_key: str, private_key: str):
        if not public_key or not private_key:
            raise ValueError("KrakenLiveAdapter requires public_key and private_key")
        self.public_key = public_key
        self.private_key = private_key

    # ─── ping ────────────────────────────────────────────────────────

    async def ping(self) -> dict:
        result = await call_private(
            "/0/private/Balance", self.public_key, self.private_key, {},
        )
        return {
            "ok": True,
            "balances": {k: float(v) for k, v in (result or {}).items()},
            "paper": False,
        }

    # ─── account / positions ─────────────────────────────────────────

    async def get_account(self) -> dict:
        result = await call_private(
            "/0/private/Balance", self.public_key, self.private_key, {},
        )
        # Translate Kraken's balance dict into a uniform shape. Equity
        # equivalent = sum of USD-denominated balances; for non-USD
        # assets, we'd need ticker math (skipped here — day-1).
        usd_keys = ("ZUSD", "USD")
        cash = 0.0
        for k in usd_keys:
            v = result.get(k)
            if v is not None:
                cash += float(v)
        return {
            "account_number": "kraken-live",
            "status": "ACTIVE",
            "equity": cash,         # day-1 approximation
            "cash": cash,
            "buying_power": cash,
            "paper": False,
        }

    async def list_positions(self) -> list[dict]:
        """Open margin positions only — spot holdings show up via Balance."""
        result = await call_private(
            "/0/private/OpenPositions", self.public_key, self.private_key, {},
        )
        positions = []
        for pos_id, p in (result or {}).items():
            positions.append({
                "symbol": p.get("pair", ""),
                "qty": float(p.get("vol", 0)),
                "side": p.get("type", ""),
                "avg_entry_price": float(p.get("cost", 0)) / max(float(p.get("vol", 0)), 1e-9),
                "market_value": float(p.get("value", 0)),
                "cost_basis": float(p.get("cost", 0)),
                "unrealized_pl": float(p.get("net", 0)),
                "position_id": pos_id,
            })
        return positions

    # ─── orders ──────────────────────────────────────────────────────

    async def submit_market_order(
        self,
        symbol: str,
        qty: Optional[float] = None,
        notional: Optional[float] = None,
        side: str = "BUY",
        client_order_id: Optional[str] = None,
        mc_receipt: Optional[dict] = None,
        leverage: Optional[int] = None,
    ) -> dict:
        """Place a market order on Kraken.

        `symbol` MUST be a Kraken-native pair name (e.g. "XBTUSD") —
        the resolver has already translated the canonical asset key
        before this method is called. The adapter does NOT do any
        identity-level resolution.

        Doctrine pin (2026-05-23): refuses to submit without an MC
        execution receipt (same bypass-blocking invariant as Alpaca).

        Paradox v3 (Step 5.b, 2026-02-22): when `leverage` is set,
        Kraken opens a margin position (long if side=buy, short if
        side=sell) instead of a spot trade. Required for v3 BEARISH
        crypto plans that refire as SHORT — without leverage Kraken
        rejects a spot sell with no existing position.
        """
        # Adapter-level bypass guard — last line of defence.
        if not isinstance(mc_receipt, dict) or not mc_receipt.get("signature") \
                or not mc_receipt.get("mc_policy_hash"):
            raise PermissionError(
                "kraken.submit_market_order refused: no MC execution "
                "receipt attached. Doctrine: every broker write must "
                "carry a signed receipt minted by MC's broker_router. "
                "NO_TRADE."
            )
        if (qty is None) == (notional is None):
            raise ValueError("supply exactly one of qty or notional")

        kraken_pair = to_kraken_pair(symbol) if "/" in symbol else symbol
        side_l = side.lower()
        if side_l not in ("buy", "sell"):
            raise ValueError(f"side must be buy/sell, got {side!r}")

        # Compute volume in BASE units from notional, if needed.
        if qty is None:
            last_price = await _ticker_price(kraken_pair)
            volume = float(notional) / last_price
        else:
            volume = float(qty)

        # Kraken minimum order sizes vary by pair; pre-emptively reject
        # absurdly tiny volumes. The adapter doesn't know per-pair
        # minimums — broker will reject with EOrder:Invalid volume if so.

        params = {
            "pair": kraken_pair,
            "type": side_l,
            "ordertype": "market",
            "volume": f"{volume:.8f}".rstrip("0").rstrip("."),
        }
        if leverage is not None and int(leverage) > 1:
            params["leverage"] = str(int(leverage))
        if client_order_id:
            # Kraken uses `userref` (uint32) — we hash the client_order_id
            # into a 32-bit number for traceability.
            params["userref"] = str(abs(hash(client_order_id)) % (2**31))

        logger.info(
            "kraken submit %s %s vol=%s userref=%s lev=%s",
            side_l, kraken_pair, params["volume"], params.get("userref"),
            params.get("leverage"),
        )

        result = await call_private(
            "/0/private/AddOrder",
            self.public_key, self.private_key,
            params,
        )

        # Kraken returns { descr: { order: "..." }, txid: ["..."] }
        txids = result.get("txid") or []
        order_id = txids[0] if txids else f"kraken-pending-{uuid.uuid4().hex[:8]}"
        descr = (result.get("descr") or {}).get("order", "")

        return {
            "order_id": order_id,
            "client_order_id": client_order_id,
            "status": "submitted",
            "submitted_at": _now_iso(),
            "filled_at": None,
            "filled_qty": 0.0,
            "filled_avg_price": None,
            "broker": "kraken",
            "broker_descr": descr,
            "pair": kraken_pair,
            "volume_base": volume,
            "side": side_l,
        }

    async def submit_limit_order(
        self,
        symbol: str,
        qty: float,
        limit_price: float,
        side: str = "BUY",
        client_order_id: Optional[str] = None,
        mc_receipt: Optional[dict] = None,
        leverage: Optional[int] = None,
    ) -> dict:
        """Paradox v3 (Step 5.b, 2026-02-22) — limit order on Kraken.

        Same doctrine pins as `submit_market_order`:
          * MC execution receipt required (bypass guard).
          * `symbol` is Kraken-native (resolver translated upstream).
          * `leverage`>1 opens a margin position (required for SHORT,
            optional for leveraged longs).

        Kraken's AddOrder uses `ordertype="limit"` + `price=<limit>`.
        """
        if not isinstance(mc_receipt, dict) or not mc_receipt.get("signature") \
                or not mc_receipt.get("mc_policy_hash"):
            raise PermissionError(
                "kraken.submit_limit_order refused: no MC execution "
                "receipt attached. NO_TRADE."
            )
        if qty is None or float(qty) <= 0:
            raise ValueError("submit_limit_order requires qty > 0")
        if limit_price is None or float(limit_price) <= 0:
            raise ValueError("submit_limit_order requires limit_price > 0")

        kraken_pair = to_kraken_pair(symbol) if "/" in symbol else symbol
        side_l = side.lower()
        if side_l not in ("buy", "sell"):
            raise ValueError(f"side must be buy/sell, got {side!r}")

        volume = float(qty)
        params = {
            "pair": kraken_pair,
            "type": side_l,
            "ordertype": "limit",
            "price": f"{float(limit_price):.5f}".rstrip("0").rstrip("."),
            "volume": f"{volume:.8f}".rstrip("0").rstrip("."),
        }
        if leverage is not None and int(leverage) > 1:
            params["leverage"] = str(int(leverage))
        if client_order_id:
            params["userref"] = str(abs(hash(client_order_id)) % (2**31))

        logger.info(
            "kraken submit LIMIT %s %s vol=%s @ %s userref=%s lev=%s",
            side_l, kraken_pair, params["volume"], params["price"],
            params.get("userref"), params.get("leverage"),
        )

        result = await call_private(
            "/0/private/AddOrder",
            self.public_key, self.private_key,
            params,
        )
        txids = result.get("txid") or []
        order_id = txids[0] if txids else f"kraken-pending-{uuid.uuid4().hex[:8]}"
        descr = (result.get("descr") or {}).get("order", "")

        return {
            "order_id": order_id,
            "client_order_id": client_order_id,
            "status": "submitted",
            "submitted_at": _now_iso(),
            "filled_at": None,
            "filled_qty": 0.0,
            "filled_avg_price": None,
            "broker": "kraken",
            "broker_descr": descr,
            "pair": kraken_pair,
            "volume_base": volume,
            "limit_price": float(limit_price),
            "side": side_l,
        }


async def get_kraken_adapter() -> Optional[KrakenLiveAdapter]:
    """Return a configured KrakenLiveAdapter, or None if not connected.

    Mirrors `get_alpaca_adapter`. The broker router calls this when
    `lane=crypto`.
    """
    keys = await get_active_keys()
    if not keys:
        return None
    public, private = keys
    try:
        return KrakenLiveAdapter(public_key=public, private_key=private)
    except ValueError:
        return None
