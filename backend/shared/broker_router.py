"""Broker router — single dispatch point.

Doctrine:
    * Lane → broker registry decides WHICH adapter handles an order.
    * Canonical asset key is the ONLY identity the brain ships.
    * Resolver translates canonical → broker-native at the last mile.
    * Every fail mode is NO_TRADE: missing lane, missing mapping, missing
      adapter, lane mismatch.

Order-routing layers (execution.py manual submit, auto_router.py) call
exactly one function here: `route_order(intent, notional_usd)`.

The router NEVER decides identity. It only:
    1. Reads `intent.lane` and `intent.symbol` (or composes from them)
    2. Looks up the broker for the lane
    3. Asks the resolver for the broker-native symbol
    4. Fetches the adapter for that broker
    5. Calls `adapter.submit_market_order(...)`
"""
from __future__ import annotations

import logging
from typing import Optional

from shared.broker.alpaca_routes import get_alpaca_adapter
from shared.crypto.broker_adapter import get_kraken_adapter
from shared.broker_symbol_resolver import (
    AssetKey,
    BrokerSymbolUnresolved,
    CanonicalError,
    LaneRoutingError,
    broker_for_lane,
    compose,
    resolve_broker_symbol,
)


logger = logging.getLogger("risedual.broker_router")


# ─────────────────────── adapter registry ───────────────────────
#
# Function-based; each broker name maps to an async `get_<broker>_adapter`
# loader. Stubs return None — that's NO_TRADE territory by design.

async def _get_public_adapter():
    return None  # not yet wired


async def _get_ibkr_adapter():
    return None  # not yet wired


ADAPTER_LOADERS = {
    "alpaca_paper": get_alpaca_adapter,
    "kraken": get_kraken_adapter,
    "public": _get_public_adapter,
    "ibkr": _get_ibkr_adapter,
}


class BrokerRouteBlocked(Exception):
    """Raised when routing cannot complete. Surfaced as a gate failure
    by the calling layer. ALWAYS NO_TRADE — fail-closed."""


# ─────────────────────── canonical composition ───────────────────────

def compose_asset(intent: dict) -> AssetKey:
    """Compose the canonical AssetKey from an intent.

    Accepts:
        - `intent.canonical` already-composed (preferred — brains
          shipping the canonical themselves)
        - `intent.symbol` + `intent.lane` (MC composes here)

    Fail-closed if neither path works.
    """
    canonical = intent.get("canonical")
    if canonical:
        # Parse back into AssetKey for type-safety downstream. We don't
        # trust the brain to know its own lane — re-derive.
        if canonical.startswith("EQ:"):
            base = canonical.split(":", 1)[1]
            return AssetKey(canonical=canonical, lane="equity", base=base, quote=None)
        if canonical.startswith("CRYPTO:"):
            tail = canonical.split(":", 1)[1]
            base, _, quote = tail.partition("-")
            return AssetKey(
                canonical=canonical, lane="crypto",
                base=base, quote=quote or "USD",
            )
        raise CanonicalError(f"unknown canonical prefix: {canonical!r}")

    symbol = intent.get("symbol")
    lane = intent.get("lane")
    return compose(symbol, lane)


# ─────────────────────── routing ───────────────────────

async def route_order(
    intent: dict,
    *,
    notional_usd: float,
    client_order_id: Optional[str] = None,
) -> dict:
    """Route a single intent's order to the correct broker.

    Returns the adapter's order-response dict on success.
    Raises BrokerRouteBlocked on any NO_TRADE condition.

    The caller (auto-router or /execution/submit) is responsible for
    running its full gate chain BEFORE calling this — the router only
    enforces broker-identity invariants, NOT trade-policy gates.
    """
    intent_id = intent.get("intent_id", "<unknown>")

    # 1. Compose canonical AssetKey.
    try:
        asset = compose_asset(intent)
    except CanonicalError as e:
        raise BrokerRouteBlocked(
            f"intent {intent_id} has no resolvable canonical asset: {e}"
        ) from e

    # 2. Pick broker by lane.
    try:
        broker_name = broker_for_lane(asset.lane)
    except LaneRoutingError as e:
        raise BrokerRouteBlocked(str(e)) from e

    # 3. Translate canonical → broker-native.
    try:
        broker_symbol = resolve_broker_symbol(asset, broker_name)
    except BrokerSymbolUnresolved as e:
        raise BrokerRouteBlocked(str(e)) from e

    # 4. Fetch the live adapter.
    loader = ADAPTER_LOADERS.get(broker_name)
    if not loader:
        raise BrokerRouteBlocked(
            f"no adapter loader registered for broker {broker_name!r}; NO_TRADE"
        )
    adapter = await loader()
    if adapter is None:
        raise BrokerRouteBlocked(
            f"broker {broker_name!r} adapter not configured (no credentials?); NO_TRADE"
        )

    # 5. Submit through the adapter.
    side = "BUY" if intent.get("action") in ("BUY", "COVER") else "SELL"
    logger.info(
        "route_order intent=%s canonical=%s lane=%s broker=%s broker_sym=%s side=%s $%.2f",
        intent_id, asset.canonical, asset.lane, broker_name, broker_symbol,
        side, notional_usd,
    )
    order = await adapter.submit_market_order(
        symbol=broker_symbol if isinstance(broker_symbol, str) else asset.base,
        notional=notional_usd,
        side=side,
        client_order_id=client_order_id,
    )
    # Stamp routing metadata so receipts can be sliced by broker / lane.
    order.setdefault("broker", broker_name)
    order["lane"] = asset.lane
    order["canonical"] = asset.canonical
    order["broker_symbol"] = broker_symbol if isinstance(broker_symbol, str) else str(broker_symbol)
    return order


# ─────────────────────── adapter peek ───────────────────────

async def adapter_for_lane(lane: str):
    """Convenience used by gate code that wants to know if a broker is
    even connected for a given lane WITHOUT submitting anything.

    Returns the adapter (truthy) or None.
    """
    try:
        broker = broker_for_lane(lane)
    except LaneRoutingError:
        return None
    loader = ADAPTER_LOADERS.get(broker)
    if not loader:
        return None
    return await loader()
