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


def _strip_canonical_prefix(symbol: str) -> str:
    """Strip an over-prefixed canonical from operator-injected input.

    The canonical format the system stores is `EQ:AAPL` / `CRYPTO:BTC-USD`
    (composed by `compose()` here). But operators sometimes paste the
    canonical form back into an intent-inject UI ("EQ:AAPL") thinking
    that's the right input format. Downstream code (the
    `symbol_in_universe` gate, the broker_symbol_map lookup) keys on
    the BARE ticker — so a prefixed input would NO_TRADE every time.

    This helper accepts ANY of:
        AAPL          → AAPL
        EQ:AAPL       → AAPL
        BTC-USD       → BTC-USD
        CRYPTO:BTC-USD→ BTC-USD
        CR:BTC-USD    → BTC-USD          (shorthand)
    and returns the bare form. Idempotent: bare input passes through.

    Operator intent (2026-02-19): "Unify intent symbol formats — EQ:AMZN
    vs AMZN mismatch in manual intent injections."
    """
    if not symbol:
        return symbol
    s = symbol.strip().upper()
    for prefix in ("CRYPTO:", "EQUITY:", "EQ:", "CR:"):
        if s.startswith(prefix):
            return s[len(prefix):]
    return s


def compose(symbol: str, lane: Optional[str]) -> AssetKey:
    """Compose a canonical AssetKey from a (symbol, lane) pair.

    The brain ships `symbol` + `lane`; MC composes the canonical here.
    This is the single conversion point — once composed, downstream code
    only reads `.canonical` / `.lane` / `.base`.

    2026-02-19: accepts already-canonical input (`EQ:AAPL`,
    `CRYPTO:BTC-USD`, `CR:BTC-USD`) and idempotently re-composes — the
    prefix is silently stripped so an operator who pastes the canonical
    form into an inject UI doesn't blow up the `symbol_in_universe`
    gate downstream.

    Fail-closed on any missing or malformed input.
    """
    if not symbol:
        raise CanonicalError("symbol is required to compose AssetKey")
    if not lane:
        raise CanonicalError("lane is required; missing lane = NO_TRADE")

    sym = _strip_canonical_prefix(symbol)
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
        # Day-1 + 2026-02-20 (operator directive): top-30 USD pairs
        # by trading volume on Kraken, mapped to Kraken's altnames.
        #
        # Kraken altname convention for USD pairs is `<BASE>USD` with
        # two well-known exceptions:
        #   * BTC  → XBTUSD  (Kraken's legacy "XBT" ISO-coded ticker)
        #   * DOGE → XDGUSD  (Kraken's legacy "XDG" ticker)
        #
        # Adding/removing pairs here is the ONLY place needed to
        # extend crypto coverage — the broker_router NO_TRADEs any
        # canonical not in this map, by design.
        "CRYPTO:BTC-USD":   "XBTUSD",
        "CRYPTO:ETH-USD":   "ETHUSD",
        "CRYPTO:SOL-USD":   "SOLUSD",
        "CRYPTO:XRP-USD":   "XRPUSD",
        "CRYPTO:DOGE-USD":  "XDGUSD",
        "CRYPTO:ADA-USD":   "ADAUSD",
        "CRYPTO:AVAX-USD":  "AVAXUSD",
        "CRYPTO:DOT-USD":   "DOTUSD",
        "CRYPTO:LINK-USD":  "LINKUSD",
        "CRYPTO:LTC-USD":   "LTCUSD",
        "CRYPTO:BCH-USD":   "BCHUSD",
        "CRYPTO:MATIC-USD": "MATICUSD",
        "CRYPTO:ATOM-USD":  "ATOMUSD",
        "CRYPTO:NEAR-USD":  "NEARUSD",
        "CRYPTO:APT-USD":   "APTUSD",
        "CRYPTO:ARB-USD":   "ARBUSD",
        "CRYPTO:OP-USD":    "OPUSD",
        "CRYPTO:UNI-USD":   "UNIUSD",
        "CRYPTO:AAVE-USD":  "AAVEUSD",
        "CRYPTO:INJ-USD":   "INJUSD",
        "CRYPTO:FIL-USD":   "FILUSD",
        "CRYPTO:ALGO-USD":  "ALGOUSD",
        "CRYPTO:XLM-USD":   "XLMUSD",
        "CRYPTO:TRX-USD":   "TRXUSD",
        "CRYPTO:TIA-USD":   "TIAUSD",
        "CRYPTO:SUI-USD":   "SUIUSD",
        "CRYPTO:SEI-USD":   "SEIUSD",
        "CRYPTO:WIF-USD":   "WIFUSD",
        "CRYPTO:PEPE-USD":  "PEPEUSD",
        "CRYPTO:SHIB-USD":  "SHIBUSD",
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
    "webull": {
        # Webull route (2026-06-10). Equities + crypto on the SAME
        # broker. Equity tickers are bare (same shape as Public.com).
        # Crypto symbols are concatenated <BASE><QUOTE> with no
        # delimiter — Webull's snapshot/order endpoints documented
        # the BTCUSD form (no dash). Day-1 universe mirrors what the
        # other equity/crypto brokers already carry, so an operator
        # can divert any existing tradable to Webull without a
        # symbol-map gap.
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
        "CRYPTO:BTC-USD": "BTCUSD",
        "CRYPTO:ETH-USD": "ETHUSD",
    },
}


class BrokerSymbolUnresolved(Exception):
    """Raised when a (canonical, broker) pair has no mapping. Treated
    as a fail-closed NO_TRADE signal by every routing layer."""


def _rule_based_webull_native(canonical: str) -> Optional[str]:
    """Derive Webull's native symbol from the canonical form using
    Webull's known conventions. Returns None if the canonical can't
    be parsed.

    Doctrine pin (2026-06-10): Webull's tradeable universe covers
    standard US-listed equities (bare ticker on the wire) and the
    major crypto pairs in concatenated form (`BTCUSD`, `ETHUSD`,
    `SOLUSD`, etc.). The static `BROKER_SYMBOL_MAP["webull"]` entries
    handle any name where the convention DOESN'T hold (e.g., a
    Webull-side rename or a non-USD quote that needs a custom map);
    everything else falls through to this rule.

    Conventions:
      * `EQ:<TICKER>`              → `<TICKER>`
      * `CRYPTO:<BASE>-<QUOTE>`    → `<BASE><QUOTE>`  (no dash, no slash)
      * `CRYPTO:<BASE>/<QUOTE>`    → `<BASE><QUOTE>`  (legacy slash form)

    This means adding a symbol to `patterns_universe` automatically
    makes it Webull-routable — no broker_symbol_resolver code change
    required for new universe entries.
    """
    s = (canonical or "").upper().strip()
    if not s:
        return None
    if s.startswith("EQ:"):
        ticker = s[3:]
        return ticker if ticker.isalnum() else None
    if s.startswith("CRYPTO:"):
        pair = s[7:]
        # Require an explicit BASE/QUOTE separator. Webull's wire form
        # is concatenated (BTCUSD), but the canonical we accept must
        # carry the quote explicitly — a bare "CRYPTO:BTC" has no
        # quote and must NOT silently resolve.
        for sep in ("-", "/"):
            if sep in pair:
                base, _, quote = pair.partition(sep)
                if base and quote and base.isalnum() and quote.isalnum():
                    return f"{base}{quote}"
        return None
    return None


def resolve_broker_symbol(asset: AssetKey, broker: str) -> Any:
    """Translate canonical → broker-native. Fail-closed.

    For brokers in `_RULE_BASED_SYMBOL_BROKERS` (Webull as of
    2026-06-10), if the static map has no explicit entry we apply
    the broker's known symbol-shape convention. This keeps the
    static map authoritative for edge cases (Webull renames, custom
    pairs) while letting the operator's full `patterns_universe`
    route through Webull without manual map maintenance.
    """
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
    if resolved is not None:
        return resolved
    # Rule-based fallback (Webull only, today).
    if broker in _RULE_BASED_SYMBOL_BROKERS:
        derived = _rule_based_webull_native(asset.canonical)
        if derived:
            return derived
    raise BrokerSymbolUnresolved(
        f"no broker mapping for {asset.canonical!r} on {broker!r}; NO_TRADE"
    )


# Brokers whose symbol shape follows a deterministic rule, so any
# canonical not in the static map can still be resolved. The static
# map remains authoritative for explicit overrides (e.g., a Webull
# rename of a ticker we'd otherwise compute wrong).
_RULE_BASED_SYMBOL_BROKERS: frozenset[str] = frozenset({"webull"})


# ───────────────────────── lane → broker registry ─────────────────────
#
# 2026-02-XX: Operator decision — Alpaca paper is REMOVED from the
# equity path. Public.com is the sole equity broker. The registry
# entry below stays as `alpaca_paper` (legacy slot name; many tests
# and call sites reference it) but the actual loader resolves to
# Public via `broker_router._get_equity_adapter`. No Alpaca fallback.

# 2026-02-19 (operator directive): Webull is the SOLE equity broker.
# Alpaca and Public.com are removed from the live routing path. The
# legacy slot name `alpaca_paper` is retained as a back-compat alias
# elsewhere, but the equity lane resolves to Webull from this deploy
# onward. `broker_router._get_equity_adapter` still exists for any
# legacy caller but now returns the Webull adapter so routing is
# consistent end-to-end.
LANE_BROKER_REGISTRY: dict[LaneT, str] = {
    "equity": "webull",
    "crypto": "kraken",
}


def equity_broker_preference() -> str:
    """Operator-controlled equity-broker preference.

    Legal values (2026-02-XX onward):
        - "public" (default + only supported value)

    Anything else falls back to "public". The previous `auto` and
    `alpaca_paper` modes were removed when the operator chose to
    drop Alpaca entirely — Public.com is now the only equity broker
    in the system. If Public is unavailable (no creds / API down)
    the broker_router fails-closed with NO_TRADE; it does NOT
    silently route to Alpaca anymore.
    """
    val = (os.environ.get("RISEDUAL_EQUITY_BROKER") or "public").strip().lower()
    if val != "public":
        # Operator typo or stale env var — log-worthy but harmless.
        # Forcing `public` here is intentional: equity ALWAYS goes
        # to Public when this preference function is consulted.
        return "public"
    return "public"


class LaneRoutingError(Exception):
    """Raised when a lane has no broker configured. NO_TRADE."""


def broker_for_lane(lane: str) -> str:
    if lane not in LANE_BROKER_REGISTRY:
        raise LaneRoutingError(
            f"lane {lane!r} has no broker registered; NO_TRADE"
        )
    return LANE_BROKER_REGISTRY[lane]
