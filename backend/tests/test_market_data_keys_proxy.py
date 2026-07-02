"""Market-data key proxy — doctrine tripwires (2026-05-28).

The market-data key proxy distributes data-source tokens (Polygon,
Finnhub, Alpha Vantage, FRED, NewsAPI, SEC user-agent) to
authenticated brain sidecars. The brain teams need this so their
sidecars can read market data without holding broker keys.

Doctrine pin (D-DATA-KEYS-2026-05-28):
  MC NEVER distributes broker keys (Alpaca, Kraken, IBKR, Coinbase,
  Binance). The boundary is enforced by TWO independent gates:
    1. WHITELIST: only fields in MARKET_DATA_KEY_FIELDS are served
    2. FORBIDDEN FRAGMENTS: any field name containing ALPACA / KRAKEN /
       IBKR / COINBASE / BINANCE / BROKER / SECRET_KEY / EXECUTE /
       TRADING_TOKEN / BROKER_TOKEN is rejected even if it's in the
       whitelist (defense in depth against future authoring errors)

These tripwires lock both gates. Future code that tries to add a
broker key field will fail loudly here. This is the most important
tripwire file in the codebase right now — it prevents the 2026-05-23
orphan-execution failure mode from re-opening via the data-keys
endpoint.
"""
from __future__ import annotations

import os

import pytest

from routes.market_data_keys import (
    FORBIDDEN_FRAGMENTS,
    KNOWN_BRAINS,
    MARKET_DATA_KEY_FIELDS,
    _validate_field_safe,
)


pytestmark = [pytest.mark.tripwire]


# ──────────────────────── WHITELIST INTEGRITY ────────────────────────


def test_whitelist_does_not_contain_alpaca():
    """ALPACA_API_KEY / ALPACA_SECRET_KEY / etc. must never appear."""
    for field in MARKET_DATA_KEY_FIELDS:
        assert "ALPACA" not in field.upper(), (
            f"DOCTRINE VIOLATION: {field!r} contains 'ALPACA' — "
            f"broker key MUST NOT be in market-data whitelist"
        )


def test_whitelist_does_not_contain_kraken():
    """KRAKEN_API_KEY / KRAKEN_API_SECRET must never appear."""
    for field in MARKET_DATA_KEY_FIELDS:
        assert "KRAKEN" not in field.upper(), (
            f"DOCTRINE VIOLATION: {field!r} contains 'KRAKEN' — "
            f"broker key MUST NOT be in market-data whitelist"
        )


def test_whitelist_does_not_contain_any_broker_provider():
    """No field in the whitelist names any known broker."""
    BROKERS = ("ALPACA", "KRAKEN", "IBKR", "COINBASE", "BINANCE", "TDAMERITRADE", "ETRADE")
    for field in MARKET_DATA_KEY_FIELDS:
        for broker in BROKERS:
            assert broker not in field.upper(), (
                f"DOCTRINE VIOLATION: {field!r} contains broker "
                f"name {broker!r} — brokers must never appear here"
            )


def test_whitelist_does_not_contain_secret_or_execute_fragments():
    """SECRET_KEY / EXECUTE / TRADING_TOKEN naming patterns are
    forbidden — these are broker-style field names."""
    bad = ("SECRET_KEY", "EXECUTE", "TRADING_TOKEN", "BROKER_TOKEN", "ORDER")
    for field in MARKET_DATA_KEY_FIELDS:
        for frag in bad:
            assert frag not in field.upper(), (
                f"DOCTRINE VIOLATION: {field!r} contains {frag!r} — "
                f"smells like a broker key, not a data key"
            )


def test_forbidden_fragments_list_contains_all_brokers():
    """The forbidden-fragments list must include every known broker
    name. If a new broker is added to the codebase, its name MUST be
    added here so the defense-in-depth check rejects it."""
    required = ("ALPACA", "KRAKEN", "IBKR", "COINBASE", "BINANCE", "BROKER")
    for req in required:
        assert req in FORBIDDEN_FRAGMENTS, (
            f"FORBIDDEN_FRAGMENTS missing {req!r} — defense-in-depth "
            f"check would not catch a poisoned whitelist entry"
        )


# ──────────────────────── _validate_field_safe ────────────────────────


def test_validate_field_safe_accepts_whitelisted_fields():
    """All currently whitelisted fields must pass the safety check."""
    for field in MARKET_DATA_KEY_FIELDS:
        assert _validate_field_safe(field) is True, (
            f"{field!r} is in the whitelist but fails the safety "
            f"check — fix the whitelist or fix the check"
        )


def test_validate_field_safe_rejects_alpaca_api_key():
    """Even if someone slips ALPACA_API_KEY into the whitelist by
    mistake, the forbidden-fragment check still rejects it."""
    # Simulate a poisoned whitelist by passing a forbidden name.
    # The whitelist check rejects (not in whitelist) AND the
    # fragment check would reject independently — both must fail.
    assert _validate_field_safe("ALPACA_API_KEY") is False
    assert _validate_field_safe("ALPACA_SECRET_KEY") is False
    assert _validate_field_safe("KRAKEN_API_KEY") is False
    assert _validate_field_safe("KRAKEN_API_SECRET") is False


def test_validate_field_safe_rejects_unlisted_fields():
    """Random field names must be rejected by the whitelist alone."""
    assert _validate_field_safe("RANDOM_TOKEN") is False
    assert _validate_field_safe("ADMIN_PASSWORD") is False
    assert _validate_field_safe("MONGO_URL") is False


# ──────────────────────── KNOWN_BRAINS ────────────────────────


def test_known_brains_matches_runtime_set():
    """The brain list must match LIVE_RUNTIMES so any sidecar that
    authenticates against /checkin can also fetch market-data keys."""
    from namespaces import LIVE_RUNTIMES
    assert set(KNOWN_BRAINS) == set(LIVE_RUNTIMES), (
        f"KNOWN_BRAINS {KNOWN_BRAINS} drifted from "
        f"LIVE_RUNTIMES {LIVE_RUNTIMES} — sidecars will get "
        f"403 on the data keys endpoint while passing /checkin"
    )


# ──────────────────────── AUTH ────────────────────────


def test_auth_rejects_missing_brain_id():
    """X-Brain-Id is required — no anonymous fetches allowed."""
    from fastapi import HTTPException
    from routes.market_data_keys import _authenticate
    with pytest.raises(HTTPException) as exc_info:
        _authenticate(None, "any-token")
    assert exc_info.value.status_code == 401


def test_auth_rejects_unknown_brain():
    """Unknown brain → 404, not 401, so misconfigured sidecars get a
    clearer error."""
    from fastapi import HTTPException
    from routes.market_data_keys import _authenticate
    with pytest.raises(HTTPException) as exc_info:
        _authenticate("bogus_brain", "any-token")
    assert exc_info.value.status_code == 404


def test_auth_rejects_token_mismatch(monkeypatch):
    """Wrong token for a known brain → 401."""
    from fastapi import HTTPException
    from routes.market_data_keys import _authenticate
    monkeypatch.setenv("BARRACUDA_INGEST_TOKEN", "the-correct-one")
    with pytest.raises(HTTPException) as exc_info:
        _authenticate("camaro", "the-wrong-one")
    assert exc_info.value.status_code == 401


def test_auth_returns_canonical_brain_on_match(monkeypatch):
    """Correct token → returns lowercase canonical brain name."""
    from routes.market_data_keys import _authenticate
    monkeypatch.setenv("BARRACUDA_INGEST_TOKEN", "matching-token")
    out = _authenticate("CAMARO", "matching-token")
    assert out == "camaro"


# ──────────────────────── INTEGRATION — full endpoint ────────────────────────


@pytest.mark.asyncio
async def test_endpoint_returns_only_whitelisted_fields(monkeypatch):
    """End-to-end: configured env vars come back via the endpoint;
    nothing else does."""
    from routes.market_data_keys import get_market_data_keys
    monkeypatch.setenv("BARRACUDA_INGEST_TOKEN", "test-token")
    monkeypatch.setenv("FINNHUB_API_KEY", "test-finnhub-key")
    monkeypatch.setenv("POLYGON_API_KEY", "test-polygon-key")
    # Set a broker key in env — must NOT come back through the proxy.
    monkeypatch.setenv("ALPACA_API_KEY", "this-must-never-leak")
    monkeypatch.setenv("KRAKEN_API_KEY", "this-must-never-leak-either")

    result = await get_market_data_keys(
        x_brain_id="camaro", x_runtime_token="test-token",
    )
    assert result["brain"] == "camaro"
    # Data keys came through
    assert result["keys"].get("FINNHUB_API_KEY") == "test-finnhub-key"
    assert result["keys"].get("POLYGON_API_KEY") == "test-polygon-key"
    # Broker keys did NOT come through (defence in depth)
    assert "ALPACA_API_KEY" not in result["keys"]
    assert "ALPACA_SECRET_KEY" not in result["keys"]
    assert "KRAKEN_API_KEY" not in result["keys"]
    assert "KRAKEN_API_SECRET" not in result["keys"]
    # Returned doctrine stamp
    assert result["doctrine"] == "market_data_only"


@pytest.mark.asyncio
async def test_endpoint_response_keys_match_whitelist_exactly(monkeypatch):
    """Every field returned by the endpoint must be in the whitelist."""
    from routes.market_data_keys import get_market_data_keys
    monkeypatch.setenv("BARRACUDA_INGEST_TOKEN", "test-token")
    # Set every possible field — including a forbidden one.
    for field in MARKET_DATA_KEY_FIELDS:
        monkeypatch.setenv(field, f"value-{field}")
    monkeypatch.setenv("ALPACA_API_KEY", "BROKER_KEY_NEVER_RETURN")

    result = await get_market_data_keys(
        x_brain_id="camaro", x_runtime_token="test-token",
    )
    for field in result["keys"]:
        assert field in MARKET_DATA_KEY_FIELDS, (
            f"Endpoint returned {field!r} which is not in whitelist"
        )
        assert "ALPACA" not in field.upper()
        assert "KRAKEN" not in field.upper()


@pytest.mark.asyncio
async def test_endpoint_does_not_return_unconfigured_fields(monkeypatch):
    """If a field has no env value, it's listed under unconfigured —
    NOT returned as an empty string in `keys`."""
    from routes.market_data_keys import get_market_data_keys
    monkeypatch.setenv("BARRACUDA_INGEST_TOKEN", "test-token")
    # Clear all data fields
    for field in MARKET_DATA_KEY_FIELDS:
        monkeypatch.delenv(field, raising=False)
    # Set just one
    monkeypatch.setenv("FINNHUB_API_KEY", "the-only-one")

    result = await get_market_data_keys(
        x_brain_id="camaro", x_runtime_token="test-token",
    )
    assert result["keys"] == {"FINNHUB_API_KEY": "the-only-one"}
    assert len(result["unconfigured_fields"]) == len(MARKET_DATA_KEY_FIELDS) - 1


@pytest.mark.asyncio
async def test_manifest_does_not_reveal_values(monkeypatch):
    """The manifest endpoint must reveal field NAMES only — no values
    leaked, no auth required (names are static)."""
    from routes.market_data_keys import get_market_data_manifest
    monkeypatch.setenv("FINNHUB_API_KEY", "should-NOT-appear")
    monkeypatch.setenv("ALPACA_API_KEY", "should-DEFINITELY-NOT-appear")

    result = await get_market_data_manifest()
    body_str = str(result)
    assert "should-NOT-appear" not in body_str
    assert "should-DEFINITELY-NOT-appear" not in body_str
    # Field names ARE in the response (the manifest's whole purpose)
    assert "FINNHUB_API_KEY" in result["fields"]
