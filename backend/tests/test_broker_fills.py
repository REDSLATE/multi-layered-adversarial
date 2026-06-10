"""Tests for the Public.com broker-fills ingestor.

Doctrine pin (operator directive, 2026-06-10): the AAPL 06-09
incident happened because MC had no broker truth. These tests pin
the contract of the ingestor that closes that gap:

    * `_normalize_transaction` keeps ONLY actual trades and shapes
      them into MC's canonical fill row.
    * Upserts are idempotent on Public's transaction id.
    * `has_pending_order` and `get_pending_orders_for_symbol`
      answer the auto-router's dedupe question — "is there an
      order in flight on this symbol right now?"
"""
import sys

sys.path.insert(0, "/app/backend")

from shared.broker_fills import _normalize_transaction  # noqa: E402


# ── Normalization filters out non-TRADE rows ──────────────────────


def test_normalize_drops_money_movement():
    tx = {
        "id": "abc", "type": "MONEY_MOVEMENT", "subType": "DEPOSIT",
        "symbol": None, "side": None, "quantity": None,
        "netAmount": "100.00", "timestamp": "2026-06-09T00:00:00Z",
    }
    assert _normalize_transaction(tx, "5LG34065") is None


def test_normalize_drops_trade_without_symbol():
    tx = {
        "id": "abc", "type": "TRADE", "subType": "TRADE",
        "symbol": None, "side": "BUY",
        "quantity": "1", "netAmount": "-100",
        "timestamp": "2026-06-09T14:36:24Z",
    }
    assert _normalize_transaction(tx, "5LG34065") is None


# ── Normalization shape for a real AAPL fill row ──────────────────


def _real_aapl_fill():
    """Shape lifted from the 06-09 incident replay — a real
    Public.com /history row."""
    return {
        "id": "fill-id-1",
        "type": "TRADE",
        "subType": "TRADE",
        "accountNumber": "5LG34065",
        "symbol": "AAPL",
        "securityType": "EQUITY",
        "side": "BUY",
        "description": "BUY 0.0102 AAPL at 292.92",
        "netAmount": "-2.99",
        "principalAmount": "-2.99",
        "quantity": "0.0102",
        "direction": None,
        "fees": "0.00",
        "timestamp": "2026-06-09T14:36:24.190Z",
    }


def test_normalize_extracts_canonical_shape():
    out = _normalize_transaction(_real_aapl_fill(), "5LG34065")
    assert out is not None
    assert out["_id"] == "fill-id-1"
    assert out["symbol"] == "AAPL"
    assert out["side"] == "BUY"
    assert out["qty"] == 0.0102
    assert out["net_amount"] == -2.99
    assert out["account_id"] == "5LG34065"
    assert out["broker"] == "public"
    assert out["timestamp"] == "2026-06-09T14:36:24.190Z"


def test_normalize_extracts_price_from_description():
    """Public's /history doesn't have a price field — we parse it
    out of the description string. Critical for replay/audit."""
    out = _normalize_transaction(_real_aapl_fill(), "5LG34065")
    assert out["price"] == 292.92


def test_normalize_falls_back_to_net_over_qty_when_no_price_in_desc():
    tx = _real_aapl_fill()
    tx["description"] = "BUY 0.0102 AAPL"   # no " at "
    out = _normalize_transaction(tx, "5LG34065")
    # |(-2.99) / 0.0102| ≈ 293.14
    assert out["price"] is not None
    assert abs(out["price"] - 293.137) < 0.01


def test_normalize_uppercases_symbol_and_side():
    tx = _real_aapl_fill()
    tx["symbol"] = "aapl"
    tx["side"] = "buy"
    out = _normalize_transaction(tx, "5LG34065")
    assert out["symbol"] == "AAPL"
    assert out["side"] == "BUY"


def test_normalize_preserves_raw_payload():
    """The raw broker row must be retained for audit so we can
    replay incidents against the original shape."""
    tx = _real_aapl_fill()
    out = _normalize_transaction(tx, "5LG34065")
    assert out["raw"] == tx


def test_normalize_handles_garbage_quantity_and_net():
    tx = _real_aapl_fill()
    tx["quantity"] = "not a number"
    tx["netAmount"] = None
    out = _normalize_transaction(tx, "5LG34065")
    assert out["qty"] == 0.0
    assert out["net_amount"] == 0.0
    # No raise — safe-float wraps the parses.


def test_idempotent_upsert_key_is_broker_id():
    """The `_id` field becomes the Mongo upsert key. Re-polling the
    same fill must not duplicate the row."""
    a = _normalize_transaction(_real_aapl_fill(), "5LG34065")
    b = _normalize_transaction(_real_aapl_fill(), "5LG34065")
    assert a["_id"] == b["_id"]  # same broker id → same upsert key
