"""Canonical asset identity + broker symbol resolver.

Doctrine:
    Brain decisions speak canonical asset keys, never raw broker tickers.
    Broker adapters TRANSLATE canonical to broker-native at the last
    possible moment. Broker symbols are never RISEDUAL truth — they
    are display strings owned by each broker.

    Invariant:  asset_key.canonical != broker_symbol_identity

Fail-closed rules:
    * Raw symbol alone is never executable.
    * Missing lane            → NO_TRADE
    * Missing broker mapping  → NO_TRADE
    * Lane mismatch           → NO_TRADE
    * Adapter never decides identity, only translates.

Canonical format:
    EQ:<TICKER>           e.g. EQ:AAPL
    CRYPTO:<BASE>-<QUOTE> e.g. CRYPTO:BTC-USD
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal, Optional


LaneT = Literal["equity", "crypto"]


# Sentinel returned by the resolver when a (canonical, broker) pair has
# no mapping. Order routers MUST treat this as NO_TRADE.
NO_TRADE_BROKER_SYMBOL_UNRESOLVED = "NO_TRADE:BROKER_SYMBOL_UNRESOLVED"


class CanonicalError(ValueError):
    """Raised when a canonical asset key cannot be composed or validated."""


@dataclass(frozen=True)
class AssetKey:
    """Canonical asset identity. The only thing brain logic should refer to."""
    canonical: str
    lane: LaneT
    base: str            # e.g. AAPL, BTC
    quote: Optional[str] # USD for crypto; None for equity

    @property
    def is_crypto(self) -> bool:
        return self.lane == "crypto"

    @property
    def is_equity(self) -> bool:
        return self.lane == "equity"


def compose(symbol: str, lane: Optional[str]) -> AssetKey:
    """Compose a canonical AssetKey from a (symbol, lane) pair.

    The brain ships `symbol` + `lane`; MC composes the canonical here.
    This is the single conversion point — once composed, downstream code
    only reads `.canonical` / `.lane` / `.base`.

    Fail-closed on any missing or malformed input.
    """
    if not symbol:
        raise CanonicalError("symbol is required to compose AssetKey")
    if not lane:
        raise CanonicalError("lane is required; missing lane = NO_TRADE")

    sym = symbol.strip().upper()
    lane_l = lane.strip().lower()

    if lane_l == "equity":
        # Equity is plain ticker. Reject anything containing punctuation.
        if not sym.isalnum():
            raise CanonicalError(
                f"equity symbol must be alphanumeric, got {sym!r}"
            )
        return AssetKey(
            canonical=f"EQ:{sym}",
            lane="equity",
            base=sym,
            quote=None,
        )

    if lane_l == "crypto":
        # Accept either "BTC" (defaults to USD quote) or "BTC-USD" /
        # "BTC/USD" (explicit). Normalize to <BASE>-<QUOTE>.
        normalized = sym.replace("/", "-")
        if "-" in normalized:
            base, _, quote = normalized.partition("-")
        else:
            base, quote = normalized, "USD"
        if not base or not quote:
            raise CanonicalError(
                f"crypto symbol must resolve to <BASE>-<QUOTE>, got {sym!r}"
            )
        return AssetKey(
            canonical=f"CRYPTO:{base}-{quote}",
            lane="crypto",
            base=base,
            quote=quote,
        )

    raise CanonicalError(
        f"lane must be 'equity' or 'crypto', got {lane!r}; lane mismatch = NO_TRADE"
    )


# ───────────────────────── broker symbol map ─────────────────────────
#
# Each broker is a dict { canonical_key -> broker-native representation }.
# Equity brokers map equity-only; crypto brokers map crypto-only.
# Cross-lane lookups (asking Kraken for an EQ:* canonical) return
# NO_TRADE_BROKER_SYMBOL_UNRESOLVED — fail-closed.
#
# Adding a new broker = adding a key here. The adapter code never asks
# "what is BTC" — it asks the resolver for the broker-native string and
# trusts the answer.

BROKER_SYMBOL_MAP: dict[str, dict[str, Any]] = {
    "alpaca_paper": {
        # Day-1 equities universe Camaro is actively trading.
        "EQ:AAPL":  "AAPL",
        "EQ:MSFT":  "MSFT",
        "EQ:GOOGL": "GOOGL",
        "EQ:NVDA":  "NVDA",
        "EQ:AMZN":  "AMZN",
        "EQ:TSLA":  "TSLA",
        "EQ:META":  "META",
        "EQ:NFLX":  "NFLX",
        "EQ:AMD":   "AMD",
        # NOTE: deliberately NO crypto entries on Alpaca. A BTC equity-
        # ticker collision with Bitcoin must NEVER resolve here. If you
        # add crypto support to Alpaca later, the BASE letters alone are
        # not enough — the canonical key carries the discriminator.
    },
    "kraken": {
        # Day-1 live crypto pairs ONLY.
        "CRYPTO:BTC-USD": "XBTUSD",   # Kraken's altname for BTC/USD
        "CRYPTO:ETH-USD": "ETHUSD",
    },
    # Stubs for brokers connecting later. Fail-closed: unresolved lookups
    # raise BrokerSymbolUnresolved. Add entries as those integrations
    # come online.
    "public": {
        # Public.com — equity broker. Symbols are bare tickers (same
        # shape as Alpaca). Populated 2026-06-07 with the Day-1 equity
        # universe so equity routing through Public works for every
        # ticker MC's `patterns_universe` watches. Crypto on Public
        # maps differently — left blank intentionally so equity
        # crypto-collisions can't resolve here.
        "EQ:AAPL":  "AAPL",
        "EQ:MSFT":  "MSFT",
        "EQ:GOOGL": "GOOGL",
        "EQ:NVDA":  "NVDA",
        "EQ:AMZN":  "AMZN",
        "EQ:TSLA":  "TSLA",
        "EQ:META":  "META",
        "EQ:NFLX":  "NFLX",
        "EQ:AMD":   "AMD",
        "EQ:AMC":   "AMC",
        "EQ:GME":   "GME",
        "EQ:HOTH":  "HOTH",
    },
    "ibkr": {
        # IBKR uses a richer contract object, not a string. Each value
        # here is the dict the IBKR adapter needs to construct a Contract.
        # Day-1: NO mappings; populate when IBKR comes online.
    },
}


class BrokerSymbolUnresolved(Exception):
    """Raised when a (canonical, broker) pair has no mapping. Treated
    as a fail-closed NO_TRADE signal by every routing layer."""


def resolve_broker_symbol(asset: AssetKey, broker: str) -> Any:
    """Translate canonical → broker-native. Fail-closed."""
    if not isinstance(asset, AssetKey):
        raise BrokerSymbolUnresolved(
            f"resolver expects AssetKey, got {type(asset).__name__}; NO_TRADE"
        )
    broker_map = BROKER_SYMBOL_MAP.get(broker)
    if broker_map is None:
        raise BrokerSymbolUnresolved(
            f"broker {broker!r} is not registered in BROKER_SYMBOL_MAP; NO_TRADE"
        )
    resolved = broker_map.get(asset.canonical)
    if resolved is None:
        raise BrokerSymbolUnresolved(
            f"no broker mapping for {asset.canonical!r} on {broker!r}; NO_TRADE"
        )
    return resolved


# ───────────────────────── lane → broker registry ─────────────────────
#
# Day-1 live routing: equities → Alpaca paper, crypto → Kraken live.
# 2026-06-07: equity lane now PREFERS Public.com when Public credentials
# are stored. The registry holds the DEFAULT broker; the actual loader
# (`broker_router.ADAPTER_LOADERS["alpaca_paper"]`) is augmented with a
# Public-first preference so flipping Public on doesn't require an
# operator to edit this dict. Doctrine pin: NEVER change the default
# fallback without an operator note — losing the Alpaca fallback closes
# equity trading entirely if Public ever drops credentials.

LANE_BROKER_REGISTRY: dict[LaneT, str] = {
    "equity": "alpaca_paper",
    "crypto": "kraken",
}


def equity_broker_preference() -> str:
    """Operator-controlled equity-broker preference. Env-driven so the
    operator can flip from Alpaca → Public (or back) without a deploy.

    Resolves to one of {"public", "alpaca_paper"}. The router calls
    `broker_for_lane("equity")` for the SLOT name; the actual loader
    that gets picked is decided in `broker_router._equity_loader()`,
    which reads THIS preference to choose between Public and Alpaca.
    """
    val = (os.environ.get("RISEDUAL_EQUITY_BROKER") or "auto").strip().lower()
    if val in {"public", "alpaca_paper", "alpaca"}:
        return "alpaca_paper" if val == "alpaca" else val
    return "auto"  # auto: prefer Public when connected, else Alpaca


class LaneRoutingError(Exception):
    """Raised when a lane has no broker configured. NO_TRADE."""


def broker_for_lane(lane: str) -> str:
    if lane not in LANE_BROKER_REGISTRY:
        raise LaneRoutingError(
            f"lane {lane!r} has no broker registered; NO_TRADE"
        )
    return LANE_BROKER_REGISTRY[lane]
