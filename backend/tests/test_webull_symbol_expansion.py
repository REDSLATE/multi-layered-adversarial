"""Tests for the rule-based Webull symbol expansion (2026-06-10).

Doctrine pin: Webull resolves any symbol in `patterns_universe`
without manual `BROKER_SYMBOL_MAP["webull"]` upkeep. The static map
stays authoritative for explicit overrides; everything else falls
through to Webull's known conventions:
    EQ:<TICKER>       → <TICKER>
    CRYPTO:<B>-<Q>    → <B><Q>
    CRYPTO:<B>/<Q>    → <B><Q>
"""
import sys

sys.path.insert(0, "/app/backend")

import pytest

from shared.broker_symbol_resolver import (
    AssetKey,
    BROKER_SYMBOL_MAP,
    BrokerSymbolUnresolved,
    _rule_based_webull_native,
    resolve_broker_symbol,
)
from shared.broker.webull import _lane_for_symbol


# ── pure rule-derivation ──────────────────────────────────────────


@pytest.mark.parametrize("canonical,expected", [
    ("EQ:AAPL",    "AAPL"),
    ("EQ:AAL",     "AAL"),
    ("EQ:ABNB",    "ABNB"),
    ("EQ:BTDR",    "BTDR"),
    ("EQ:AVAH",    "AVAH"),
    ("EQ:GOOG",    "GOOG"),
    ("eq:msft",    "MSFT"),  # case-insensitive
    ("CRYPTO:BTC-USD",  "BTCUSD"),
    ("CRYPTO:ETH-USD",  "ETHUSD"),
    ("CRYPTO:BNB-USD",  "BNBUSD"),
    ("CRYPTO:SOL-USD",  "SOLUSD"),
    ("CRYPTO:BTC/USD",  "BTCUSD"),   # legacy slash form
    ("CRYPTO:ETH/USDT", "ETHUSDT"),
])
def test_rule_based_native_derivation(canonical, expected):
    assert _rule_based_webull_native(canonical) == expected


@pytest.mark.parametrize("canonical", [
    "AAPL",          # missing prefix
    "EQ:",           # empty ticker
    "CRYPTO:",       # empty pair
    "CRYPTO:BTC",    # missing quote
    "",
    None,
])
def test_rule_based_native_returns_none_on_garbage(canonical):
    assert _rule_based_webull_native(canonical) is None


# ── full resolver with fallback ───────────────────────────────────


def _asset(canonical: str, base: str, lane: str) -> AssetKey:
    quote = "USD" if lane == "crypto" else None
    return AssetKey(canonical=canonical, base=base, lane=lane, quote=quote)


def test_resolver_uses_static_map_when_present():
    """A canonical that IS in BROKER_SYMBOL_MAP['webull'] resolves
    via the map, not the rule. This lets the operator override the
    rule for edge cases (e.g., a Webull rename)."""
    # AAPL is explicitly in the static map → returns the mapped val
    assert "EQ:AAPL" in BROKER_SYMBOL_MAP["webull"]
    out = resolve_broker_symbol(_asset("EQ:AAPL", "AAPL", "equity"), "webull")
    assert out == "AAPL"


def test_resolver_falls_through_to_rule_for_unmapped_equity():
    """A canonical NOT in the static map but parseable by the rule
    still resolves — the operator's watchlist symbols (AAL, ABNB,
    BTDR, etc.) don't need manual map entries anymore."""
    # AAL is in patterns_universe but NOT in BROKER_SYMBOL_MAP["webull"]
    assert "EQ:AAL" not in BROKER_SYMBOL_MAP["webull"]
    out = resolve_broker_symbol(_asset("EQ:AAL", "AAL", "equity"), "webull")
    assert out == "AAL"


def test_resolver_falls_through_to_rule_for_unmapped_crypto():
    """Crypto pairs not in the static map (e.g., BNB-USD, SOL-USD)
    resolve via the rule to their concat form."""
    out = resolve_broker_symbol(
        _asset("CRYPTO:BNB-USD", "BNB-USD", "crypto"), "webull",
    )
    assert out == "BNBUSD"


def test_resolver_still_fails_on_unparseable_canonical():
    """Defense in depth: a canonical the rule can't derive must
    still raise — we don't silently invent ticker shapes."""
    with pytest.raises(BrokerSymbolUnresolved):
        resolve_broker_symbol(_asset("WEIRD:THING", "THING", "equity"), "webull")


def test_resolver_rule_doesnt_apply_to_other_brokers():
    """Public.com / Kraken / Alpaca don't get rule-based fallback —
    they have curated maps with specific symbol shapes. A canonical
    not in their map must NOT be silently filled in by the Webull
    rule."""
    # Use a canonical that's NOT in the public map
    with pytest.raises(BrokerSymbolUnresolved):
        resolve_broker_symbol(
            _asset("EQ:UNKNOWNTICKER", "UNKNOWNTICKER", "equity"), "public",
        )


# ── adapter lane heuristic ────────────────────────────────────────


@pytest.mark.parametrize("symbol,expected", [
    # Known-mapped equities
    ("AAPL",  "equity"),
    ("MSFT",  "equity"),
    # Heuristic: 1-5 letters alpha → equity
    ("AAL",   "equity"),
    ("ABNB",  "equity"),
    ("GOOG",  "equity"),
    # Crypto by USD/USDT suffix
    ("BTCUSD",  "crypto"),
    ("ETHUSD",  "crypto"),
    ("SOLUSD",  "crypto"),
    ("BNBUSD",  "crypto"),
    ("ETHUSDT", "crypto"),
])
def test_lane_heuristic_classifies_correctly(symbol, expected):
    assert _lane_for_symbol(symbol) == expected


def test_lane_heuristic_returns_none_for_unclassifiable():
    """Symbols that don't fit either pattern must return None so the
    adapter fails closed."""
    assert _lane_for_symbol("") is None
    assert _lane_for_symbol("123") is None
    assert _lane_for_symbol("$$$") is None
