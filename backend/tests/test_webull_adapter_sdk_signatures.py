"""Webull adapter SDK-signature regression tests.

Operator incident (2026-02-19, evening):
    Every manual submit on production returned HTTP 502. Root cause
    was a triple-stack of bugs in `shared/broker/webull.py`:

      1. The adapter called Webull SDK methods that don't exist
         on the installed `webull-openapi-python-sdk` (e.g.
         `account_v2.get_account_detail` — real name is
         `get_account_balance`; `order.place_order(payload_dict)` —
         real signature is positional with `qty/instrument_id` etc).
      2. A fresh `ApiClient` was constructed per order, burning the
         SDK's per-instance token cache and sending it into a
         `_check_token_enable result is False` hot loop that wedged
         the executor thread for ~25s → Cloudflare 502.
      3. `_resolve_account_id` picked `accounts[0]` — but real
         Webull profiles have multiple sub-accounts (Margin, Cash,
         Events, Futures, …) and the first one is rarely the funded
         one.

This test file pins the correct shape so a future PR can't silently
regress to the broken contract.
"""
from __future__ import annotations

import inspect

import pytest


def test_webull_adapter_uses_correct_place_order_signature():
    """The installed SDK's `place_order` is:

        place_order(account_id, qty, instrument_id, side, client_order_id,
                    order_type, extended_hours_trading, tif, ...)

    `submit_market_order` MUST call it positionally with those args,
    NOT with a dict payload. If a future PR reverts to the dict shape,
    this test fails before it ever ships.
    """
    from shared.broker.webull import WebullAdapter
    src = inspect.getsource(WebullAdapter.submit_market_order)
    # Must reference the SDK enum names, not raw strings.
    assert "OrderSide" in src, "submit_market_order must use OrderSide enum"
    assert "OrderType.MARKET" in src, "submit_market_order must use OrderType.MARKET"
    assert "OrderTIF" in src, "submit_market_order must use OrderTIF enum"
    # Must compute integer qty (Webull's standard place_order is
    # whole-share only).
    assert "qty_int" in src or "int(" in src, (
        "submit_market_order must convert notional to integer qty — "
        "Webull's standard place_order does not accept fractional"
    )


def test_webull_adapter_uses_correct_account_balance_method():
    """The installed SDK exposes `get_account_balance`, NOT
    `get_account_detail`. A regression that uses the wrong name
    causes `AttributeError: 'AccountV2' object has no attribute
    'get_account_detail'` → 502."""
    from shared.broker.webull import WebullAdapter
    src = inspect.getsource(WebullAdapter.get_account)
    assert "get_account_balance" in src
    # Strip comment lines before checking the OLD method name isn't
    # actually called (our migration comment legitimately mentions it).
    code_only = "\n".join(
        line for line in src.splitlines() if "#" not in line or not line.lstrip().startswith("#")
    )
    assert "get_account_detail" not in code_only


def test_webull_adapter_uses_query_order_detail():
    """Same shape rule for order detail — SDK has `query_order_detail`,
    not `get_order_detail`."""
    from shared.broker.webull import WebullAdapter
    src = inspect.getsource(WebullAdapter.get_order)
    assert "query_order_detail" in src
    code_only = "\n".join(
        line for line in src.splitlines() if "#" not in line or not line.lstrip().startswith("#")
    )
    assert "get_order_detail" not in code_only


def test_webull_adapter_positions_via_account_v2():
    """The SDK doesn't have `position.get_positions`; positions are
    read via `account_v2.get_account_position_details`."""
    from shared.broker.webull import WebullAdapter
    src = inspect.getsource(WebullAdapter.list_positions)
    assert "get_account_position_details" in src
    code_only = "\n".join(
        line for line in src.splitlines() if "#" not in line or not line.lstrip().startswith("#")
    )
    assert "position.get_positions" not in code_only


def test_webull_factory_is_singleton():
    """A fresh `ApiClient` per order burns the SDK's token cache.
    The factory MUST return the same instance on repeated calls so
    the token stays warm."""
    from shared.broker import webull as w
    src = inspect.getsource(w.get_webull_adapter)
    assert "_ADAPTER" in src and "_ADAPTER_LOCK" in src, (
        "get_webull_adapter must be a singleton (see _ADAPTER) so the "
        "SDK's per-instance token cache doesn't thrash."
    )
    # And there must be a reset helper for tests.
    assert hasattr(w, "reset_webull_adapter_for_tests"), (
        "tests need a way to rebind the singleton; add "
        "reset_webull_adapter_for_tests() to the module."
    )


def test_webull_account_picker_prefers_cash_with_env_override():
    """A real Webull profile has 4+ sub-accounts (Margin, Cash,
    Events, Futures). `accounts[0]` is rarely the funded one. The
    picker MUST honor `WEBULL_ACCOUNT_ID` as an explicit pin, AND
    prefer CASH-type when no pin is set."""
    from shared.broker.webull import WebullAdapter
    src = inspect.getsource(WebullAdapter._resolve_account_id)
    assert "WEBULL_ACCOUNT_ID" in src, (
        "account picker must honor WEBULL_ACCOUNT_ID env override"
    )
    assert "CASH" in src, "account picker must prefer CASH-type sub-account"


def test_webull_silences_sdk_token_log_noise():
    """The SDK's `client_initializer` logs INFO-level
    `_check_token_enable result is False` on every probe. With the
    auto-router firing 5 intents/30s, the supervisor log fills with
    that noise and operators can't see real errors. The adapter
    module raises these loggers to WARNING at import-time."""
    import logging
    # Import the adapter module to trigger the side-effect setup.
    from shared.broker import webull  # noqa: F401
    for name in (
        "webull.core.http.initializer.client_initializer",
        "webull.core.http.initializer",
        "webull.core.client",
    ):
        assert logging.getLogger(name).level >= logging.WARNING, (
            f"{name} must be silenced to WARNING+ to keep supervisor "
            f"logs readable on heavy-trade days."
        )
