"""Pytests for Kraken order-status reconciliation (2026-07-06).

Two layers of coverage:

1. **Adapter response mapper** — `shared.crypto.kraken._normalize_kraken_order`.
   Uses fixture dicts modeled EXACTLY on Kraken's REST API
   `/0/private/QueryOrders` documented response format. These
   fixtures are the ONLY place Kraken's schema is decoded, so any
   drift in Kraken's response fields will surface here first (as a
   test failure) rather than silently corrupting reconciliation
   Monday.

2. **Manual reconcile endpoint** — `routes/kraken_manual_reconcile.py`.
   Uses the SAME state-machine transitions the auto_router equity
   sweep uses (Filled / Terminal / Transient-under-cap / Working),
   so operator manual reconciliation and future auto-sweep
   promotion produce identical outcomes.

Live-verification gap (accepted 2026-07-06 per Option C):
    These tests exercise the adapter with FIXTURE responses, not
    real Kraken responses. First real Kraken response contact is
    the operator's Monday manual invocation of this endpoint. That
    is a deliberate operator action, not a background firehose.
    Promote to auto-sweep after Monday's data confirms the
    fixtures match reality.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


# ══════════════════════════════════════════════════════════════════
# Kraken response fixtures (from Kraken /0/private/QueryOrders docs)
# ══════════════════════════════════════════════════════════════════

def _kraken_filled_response():
    """Order fully filled — Kraken returns status='closed' with
    vol_exec matching vol."""
    return {
        "status": "closed",
        "opentm": 1616666559.8974,
        "starttm": 0,
        "expiretm": 0,
        "descr": {
            "pair": "XBTUSD",
            "type": "buy",
            "ordertype": "limit",
            "price": "50000.00",
        },
        "vol": "0.00020000",
        "vol_exec": "0.00020000",
        "cost": "10.05",
        "fee": "0.03",
        "price": "50250.00",
        "misc": "",
        "oflags": "fciq",
        "reason": None,
        "closetm": 1616666600.1234,
    }


def _kraken_canceled_response():
    """Order canceled by operator or exchange. Kraken populates
    `reason` on cancel/expire."""
    return {
        "status": "canceled",
        "opentm": 1616666559.8974,
        "descr": {
            "pair": "XBTUSD",
            "type": "buy",
            "ordertype": "limit",
            "price": "48000.00",
        },
        "vol": "0.00020000",
        "vol_exec": "0.00000000",
        "cost": "0.00",
        "price": "0.00",
        "reason": "User requested",
        "closetm": 1616666570.0,
    }


def _kraken_open_response():
    """Order still working — sweep should no-op."""
    return {
        "status": "open",
        "opentm": 1616666559.8974,
        "descr": {
            "pair": "XBTUSD",
            "type": "buy",
            "ordertype": "limit",
            "price": "48000.00",
        },
        "vol": "0.00020000",
        "vol_exec": "0.00000000",
        "cost": "0.00",
        "price": "0.00",
        "reason": None,
    }


def _kraken_expired_response():
    return {
        "status": "expired",
        "opentm": 1616666559.8974,
        "descr": {"pair": "XBTUSD", "type": "buy", "ordertype": "limit"},
        "vol": "0.00020000",
        "vol_exec": "0.00000000",
        "cost": "0.00",
        "price": "0.00",
        "reason": "Order expired",
        "closetm": 1616666620.0,
    }


# ══════════════════════════════════════════════════════════════════
# 1. Adapter response mapper
# ══════════════════════════════════════════════════════════════════

def test_normalize_kraken_closed_maps_to_FILLED():
    from shared.crypto.kraken import _normalize_kraken_order
    out = _normalize_kraken_order("TXID-ABC", _kraken_filled_response())
    assert out["status"] == "FILLED"
    assert out["filled_qty"] == 0.0002
    assert out["filled_avg_price"] == 50250.00
    assert out["filled_at"] is not None
    assert "2021-03-25" in out["filled_at"]  # closetm 1616666600 = 2021-03-25
    assert out["reject_reason"] is None
    assert out["txid"] == "TXID-ABC"
    # `raw` preserves the exchange-side dict for forensics.
    assert out["raw"]["status"] == "closed"


def test_normalize_kraken_canceled_maps_to_CANCELED_with_reason():
    from shared.crypto.kraken import _normalize_kraken_order
    out = _normalize_kraken_order("TXID-CXL", _kraken_canceled_response())
    assert out["status"] == "CANCELED"
    # Filled fields cleared on cancel — we don't want a partial vol_exec
    # bleed on to something that never actually filled meaningfully.
    assert out["filled_qty"] is None
    assert out["filled_avg_price"] is None
    assert out["filled_at"] is None
    assert out["reject_reason"] == "User requested"


def test_normalize_kraken_expired_maps_to_EXPIRED():
    from shared.crypto.kraken import _normalize_kraken_order
    out = _normalize_kraken_order("TXID-EXP", _kraken_expired_response())
    assert out["status"] == "EXPIRED"
    assert out["reject_reason"] == "Order expired"


def test_normalize_kraken_open_maps_to_WORKING():
    from shared.crypto.kraken import _normalize_kraken_order
    out = _normalize_kraken_order("TXID-OPEN", _kraken_open_response())
    assert out["status"] == "WORKING"
    assert out["filled_qty"] is None
    assert out["filled_avg_price"] is None


def test_normalize_kraken_unknown_status_defaults_to_WORKING():
    """Defensive default: if Kraken ships a new status value we
    haven't mapped, treat it as WORKING (safe: keeps polling)
    rather than crashing or mis-terminaling."""
    from shared.crypto.kraken import _normalize_kraken_order
    weird = {"status": "some_new_kraken_state_2027",
             "vol_exec": "0.5", "price": "100"}
    out = _normalize_kraken_order("TXID-WEIRD", weird)
    assert out["status"] == "WORKING"


def test_normalize_kraken_malformed_numeric_fields_yield_none():
    """Kraken sometimes returns empty string or missing numeric
    fields. Mapper must survive without crashing."""
    from shared.crypto.kraken import _normalize_kraken_order
    junk = {"status": "closed", "vol_exec": "not-a-number",
            "price": None, "closetm": "not-a-timestamp"}
    out = _normalize_kraken_order("TXID-JUNK", junk)
    assert out["status"] == "FILLED"
    assert out["filled_qty"] is None
    assert out["filled_avg_price"] is None
    assert out["filled_at"] is None


# ══════════════════════════════════════════════════════════════════
# 2. Manual reconcile endpoint state transitions
# ══════════════════════════════════════════════════════════════════

def _crypto_submitted_intent(intent_id="rec-cr-1", txid="TXID-KRK-1",
                             retry=0):
    return {
        "intent_id": intent_id,
        "symbol": "XBTUSD",
        "action": "BUY",
        "lane": "crypto",
        "stack": "camino",
        "gate_state": "submitted",
        "broker_order": {"id": txid, "broker": "kraken", "status": "submitted"},
        "submit_retry_count": retry,
    }


class _FakeIntentColl:
    def __init__(self, doc):
        self._doc = doc
        self.updates = []

    async def find_one(self, query, projection=None):
        # Match by intent_id or broker_order.id (both keys the
        # endpoint uses).
        if self._doc is None:
            return None
        if "intent_id" in query and query["intent_id"] == self._doc.get("intent_id"):
            return dict(self._doc)
        if "broker_order.id" in query and query["broker_order.id"] == self._doc.get("broker_order", {}).get("id"):
            return dict(self._doc)
        return None

    async def update_one(self, query, update):
        self.updates.append({"query": query, "update": update})


def _patch_endpoint(intent_doc, kraken_response, adapter_raises=None,
                    keys_return=("pub_key", "priv_key")):
    """Context stack for the manual reconcile endpoint tests."""
    from contextlib import ExitStack
    from unittest.mock import MagicMock
    stack = ExitStack()
    coll = _FakeIntentColl(intent_doc)
    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(return_value=coll)
    stack.enter_context(patch(
        "routes.kraken_manual_reconcile.db", new=fake_db,
    ))
    stack.enter_context(patch(
        "shared.crypto.kraken.get_active_keys",
        new=AsyncMock(return_value=keys_return),
    ))
    if adapter_raises is not None:
        stack.enter_context(patch(
            "shared.crypto.kraken.query_order",
            new=AsyncMock(side_effect=adapter_raises),
        ))
    else:
        # Mock query_order at the module the endpoint imports it from,
        # so the endpoint's `from ... import query_order` sees the mock.
        stack.enter_context(patch(
            "shared.crypto.kraken.query_order",
            new=AsyncMock(return_value=kraken_response),
        ))
    return stack, coll


@pytest.mark.asyncio
async def test_manual_reconcile_filled_updates_intent():
    from routes.kraken_manual_reconcile import reconcile_intent, ReconcileIn

    intent = _crypto_submitted_intent(intent_id="cr-1", txid="TX-1")
    kraken_response = {
        "status": "FILLED", "filled_qty": 0.0002,
        "filled_avg_price": 50250.0,
        "filled_at": "2021-03-25T13:23:20+00:00",
        "reject_reason": None, "txid": "TX-1", "raw": {},
    }
    stack, coll = _patch_endpoint(intent, kraken_response)
    with stack:
        result = await reconcile_intent(
            ReconcileIn(intent_id="cr-1"),
            _user={"email": "op@risedual.io"},
        )

    assert result["action_taken"] == "filled"
    assert result["new_gate_state"] == "filled"
    assert len(coll.updates) == 1
    set_doc = coll.updates[0]["update"]["$set"]
    assert set_doc["gate_state"] == "filled"
    assert set_doc["reconciled_manually"] is True
    assert set_doc["reconciled_by"] == "op@risedual.io"
    assert set_doc["broker_order.filled_qty"] == 0.0002


@pytest.mark.asyncio
async def test_manual_reconcile_open_is_no_op():
    from routes.kraken_manual_reconcile import reconcile_intent, ReconcileIn

    intent = _crypto_submitted_intent(intent_id="cr-2", txid="TX-2")
    kraken_response = {
        "status": "WORKING", "filled_qty": None,
        "filled_avg_price": None, "filled_at": None,
        "reject_reason": None, "txid": "TX-2", "raw": {},
    }
    stack, coll = _patch_endpoint(intent, kraken_response)
    with stack:
        result = await reconcile_intent(
            ReconcileIn(intent_id="cr-2"),
            _user={"email": "op@risedual.io"},
        )

    assert result["action_taken"] == "no_change"
    assert len(coll.updates) == 0


@pytest.mark.asyncio
async def test_manual_reconcile_canceled_transient_requeues_pending():
    from routes.kraken_manual_reconcile import reconcile_intent, ReconcileIn

    intent = _crypto_submitted_intent(intent_id="cr-3", txid="TX-3", retry=0)
    kraken_response = {
        "status": "CANCELED", "filled_qty": None,
        "filled_avg_price": None, "filled_at": None,
        # "rate limit" phrase → classify() → rate_limited (transient)
        "reject_reason": "rate limit exceeded — please slow down",
        "txid": "TX-3", "raw": {},
    }
    stack, coll = _patch_endpoint(intent, kraken_response)
    with stack:
        result = await reconcile_intent(
            ReconcileIn(intent_id="cr-3"),
            _user={"email": "op@risedual.io"},
        )

    assert result["action_taken"] == "rejected_retry"
    assert result["new_gate_state"] == "pending"
    set_doc = coll.updates[0]["update"]["$set"]
    unset_doc = coll.updates[0]["update"]["$unset"]
    assert set_doc["gate_state"] == "pending"
    assert set_doc["submit_retry_count"] == 1
    assert "broker_order" in unset_doc


@pytest.mark.asyncio
async def test_manual_reconcile_canceled_terminal_bucket_final():
    from routes.kraken_manual_reconcile import reconcile_intent, ReconcileIn

    intent = _crypto_submitted_intent(intent_id="cr-4", txid="TX-4")
    kraken_response = {
        "status": "CANCELED", "filled_qty": None,
        "filled_avg_price": None, "filled_at": None,
        # Kraken's canonical insufficient-funds string. Terminal bucket.
        "reject_reason": "EOrder:Insufficient funds",
        "txid": "TX-4", "raw": {},
    }
    stack, coll = _patch_endpoint(intent, kraken_response)
    with stack:
        result = await reconcile_intent(
            ReconcileIn(intent_id="cr-4"),
            _user={"email": "op@risedual.io"},
        )

    assert result["action_taken"] == "rejected_terminal"
    assert result["new_gate_state"] == "broker_rejected"
    set_doc = coll.updates[0]["update"]["$set"]
    assert set_doc["gate_state"] == "broker_rejected"
    assert set_doc["broker_reason"] == "insufficient_funds"
    assert set_doc["broker_error_terminal"] is True


@pytest.mark.asyncio
async def test_manual_reconcile_missing_credentials_returns_no_credentials():
    from routes.kraken_manual_reconcile import reconcile_intent, ReconcileIn

    intent = _crypto_submitted_intent(intent_id="cr-5", txid="TX-5")
    stack, coll = _patch_endpoint(
        intent, kraken_response={}, keys_return=None,
    )
    with stack:
        result = await reconcile_intent(
            ReconcileIn(intent_id="cr-5"),
            _user={"email": "op@risedual.io"},
        )

    assert result["action_taken"] == "no_credentials"
    assert len(coll.updates) == 0


@pytest.mark.asyncio
async def test_manual_reconcile_equity_intent_rejected():
    """This endpoint handles crypto only. An equity intent must be
    rejected with 400 so operator uses the auto-sweep for equity."""
    from fastapi import HTTPException
    from routes.kraken_manual_reconcile import reconcile_intent, ReconcileIn

    equity_intent = {
        "intent_id": "eq-1", "lane": "equity", "gate_state": "submitted",
        "broker_order": {"id": "WBULL-1"}, "submit_retry_count": 0,
    }
    stack, coll = _patch_endpoint(equity_intent, kraken_response={})
    with stack:
        try:
            await reconcile_intent(
                ReconcileIn(intent_id="eq-1"),
                _user={"email": "op@risedual.io"},
            )
            assert False, "expected HTTPException for equity lane"
        except HTTPException as e:
            assert e.status_code == 400
            assert "crypto only" in e.detail


@pytest.mark.asyncio
async def test_manual_reconcile_intent_not_found():
    from routes.kraken_manual_reconcile import reconcile_intent, ReconcileIn

    stack, coll = _patch_endpoint(None, kraken_response={})
    with stack:
        result = await reconcile_intent(
            ReconcileIn(intent_id="does-not-exist"),
            _user={"email": "op@risedual.io"},
        )

    assert result["action_taken"] == "not_found"
    assert result["found_intent"] is False
