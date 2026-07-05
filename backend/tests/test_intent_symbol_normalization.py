"""Regression: canonical-prefix normalization across the intent path.

Operator note (2026-02-19, P2 backlog cleanup):
    "Unify intent symbol formats — EQ:AMZN vs AMZN mismatch in manual
    intent injections."

The system stores BARE tickers on `intent.symbol` and the canonical
prefixed form on `intent.canonical`. The `symbol_in_universe` gate
queries `patterns_universe` by bare ticker. Manual operator
injections sometimes carry the already-canonical form ("EQ:AAPL",
"CRYPTO:BTC-USD") because the UI's preset menu lists the canonical
shape. Without normalization, the gate would NO_TRADE every operator-
injected intent that copied the canonical form back into the form.

Fix (belt-and-braces):
  * `broker_symbol_resolver._strip_canonical_prefix` — normalization
    helper, idempotent on bare input.
  * `broker_symbol_resolver.compose` — accepts prefixed input.
  * `shared/execution.py:_evaluate_gates` — strips the prefix
    before the universe lookup.
  * `shared/intents.py:post_intent` / `admin_post_intent` — strips
    the prefix at the ingestion boundary so the persisted row is
    always the bare form.
  * `frontend/src/components/OperatorInjectIntent.jsx` — strips on
    send so the wire payload matches the backend's stored form.

This test file pins the helper + the `compose` idempotency.
"""
from __future__ import annotations

import pytest

from shared.broker_symbol_resolver import _strip_canonical_prefix, compose


# ─── _strip_canonical_prefix ─────────────────────────────────────────

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("AAPL", "AAPL"),
        ("aapl", "AAPL"),
        (" AAPL ", "AAPL"),
        ("EQ:AAPL", "AAPL"),
        ("eq:aapl", "AAPL"),
        ("EQUITY:AAPL", "AAPL"),
        ("CR:BTC-USD", "BTC-USD"),
        ("CRYPTO:BTC-USD", "BTC-USD"),
        ("crypto:eth-usd", "ETH-USD"),
        # Already-bare crypto pairs are unchanged.
        ("BTC-USD", "BTC-USD"),
        # Empty / None — pass through.
        ("", ""),
    ],
)
def test_strip_canonical_prefix_table(raw, expected):
    assert _strip_canonical_prefix(raw) == expected


def test_strip_canonical_prefix_idempotent():
    """Calling the stripper twice MUST equal calling it once.

    Catches future regressions where someone "helpfully" calls the
    stripper at multiple layers and double-mangles ETH-USD → THUSD.
    """
    for raw in ("EQ:AAPL", "CRYPTO:BTC-USD", "AAPL", "BTC-USD"):
        once = _strip_canonical_prefix(raw)
        twice = _strip_canonical_prefix(once)
        assert once == twice, f"{raw!r}: not idempotent ({once!r} → {twice!r})"


# ─── compose() accepts prefixed input ────────────────────────────────

def test_compose_equity_accepts_bare_ticker():
    key = compose("AAPL", "equity")
    assert key.canonical == "EQ:AAPL"
    assert key.base == "AAPL"
    assert key.lane == "equity"


def test_compose_equity_accepts_canonical_input():
    """If the operator pastes the already-canonical form, compose()
    must NOT double-prefix it to "EQ:EQ:AAPL"."""
    key = compose("EQ:AAPL", "equity")
    assert key.canonical == "EQ:AAPL"
    assert key.base == "AAPL"


def test_compose_crypto_accepts_canonical_input():
    key = compose("CRYPTO:BTC-USD", "crypto")
    assert key.canonical == "CRYPTO:BTC-USD"
    assert key.base == "BTC"
    assert key.quote == "USD"
    assert key.lane == "crypto"


def test_compose_crypto_accepts_short_prefix():
    key = compose("CR:ETH-USD", "crypto")
    assert key.canonical == "CRYPTO:ETH-USD"
    assert key.base == "ETH"
    assert key.quote == "USD"


def test_compose_crypto_accepts_bare_pair():
    key = compose("BTC-USD", "crypto")
    assert key.canonical == "CRYPTO:BTC-USD"
    assert key.base == "BTC"
    assert key.quote == "USD"


def test_compose_crypto_accepts_slash_separator():
    """Pre-existing contract — `BTC/USD` is normalized to `BTC-USD`."""
    key = compose("BTC/USD", "crypto")
    assert key.canonical == "CRYPTO:BTC-USD"


# ─── universe gate uses the bare form ────────────────────────────────
# (Historical test `test_symbol_in_universe_gate_strips_prefix_before_lookup`
# removed 2026-07-04 — imported `shared.execution` which was deleted in the
# 2026-07-01 refactor. Behavior is covered by the `compose` and
# `_strip_canonical_prefix` tests above.)
