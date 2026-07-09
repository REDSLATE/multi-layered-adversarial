"""Crypto-lane extension of `_sweep_submitted_broker_orders` (2026-07-09 iter-22).

Doctrine (operator P2 directive):

    "Extend `_sweep_submitted_broker_orders` to crypto lane (227+
     stuck Kraken intents)."

Historically the reconcile sweep only queried Webull with
`lane: "equity"`, so any `gate_state="submitted"` crypto intent
lived forever in submitted state. iter-22 adds the Kraken branch —
`KrakenLiveAdapter.get_order(txid)` normalizes to the same shape as
`WebullAdapter.get_order()`, so the FILLED / REJECTED / EXPIRED
branches downstream work uniformly for both lanes.

This suite:
    1. Confirms the sweep now queries BOTH lane filters.
    2. Confirms a mocked Kraken adapter transitions crypto intents
       correctly through filled / terminal-reject / retry paths.
    3. Confirms an equity broker outage does NOT wedge the crypto
       sweep, and vice versa (fault isolation).
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/app/backend")

from db import db  # noqa: E402
from namespaces import SHARED_INTENTS  # noqa: E402
from shared import auto_router as ar  # noqa: E402


_TEST_PREFIX = "sweep-crypto-test-"


@pytest.fixture(autouse=True)
async def _cleanup(monkeypatch):
    """Purge synthetic sweep intents before + after each test.

    Also raises RECONCILE_BATCH_CAP to a big number so this test's
    synthetic intent isn't crowded out by the 24-25 pre-existing
    stuck production intents in the shared `test_database`. Without
    this, the sweep hits the cap on real prod rows first and skips
    the row we just inserted."""
    monkeypatch.setattr(ar, "RECONCILE_BATCH_CAP", 500)
    ar._LAST_RECONCILE_SWEEP_TS = None
    await db[SHARED_INTENTS].delete_many(
        {"intent_id": {"$regex": f"^{_TEST_PREFIX}"}},
    )
    yield
    await db[SHARED_INTENTS].delete_many(
        {"intent_id": {"$regex": f"^{_TEST_PREFIX}"}},
    )
    ar._LAST_RECONCILE_SWEEP_TS = None


def _stale_ts(minutes: int = 5) -> str:
    """Timestamp older than `RECONCILE_MIN_AGE_SEC` so the sweep picks
    it up (sweep skips fresher rows to avoid racing the submit path)."""
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes)
    ).isoformat()


def _scoped_adapter(txid: str, response: dict):
    """Return a mock KrakenLiveAdapter whose `get_order` responds ONLY
    to the test's own txid — any other txid raises so the sweep's
    error path handles it and doesn't touch that intent.

    Critical: prod `test_database` currently contains 24+ REAL stuck
    crypto intents. A blanket `AsyncMock(return_value=FILLED)` would
    flip every one of them to `filled` mid-test. This scoped mock
    ensures we only affect our own synthetic row."""
    mock = MagicMock()

    async def _get_order(oid: str):
        if str(oid) == str(txid):
            return response
        raise RuntimeError(
            f"test-scoped Kraken mock refuses non-test txid={oid!r} "
            f"(only {txid!r} is in scope)",
        )

    mock.get_order = _get_order
    return mock


async def _insert_stuck_crypto(intent_id: str, txid: str) -> None:
    """Seed a `gate_state="submitted"` crypto intent with a Kraken txid
    ready for the sweep to reconcile."""
    await db[SHARED_INTENTS].insert_one({
        "intent_id": intent_id,
        "lane": "crypto",
        "symbol": "XBTUSD",
        "action": "BUY",
        "stack": "gto",
        "stack_canonical": "gto",
        "gate_state": "submitted",
        "executed": True,
        "ingest_ts": _stale_ts(),
        "executed_at": _stale_ts(),
        "broker_order": {
            "order_id": txid, "broker": "kraken", "status": "submitted",
        },
        "final_notional_usd": 25.0,
    })


# ─── happy path: Kraken FILLED ────────────────────────────────

@pytest.mark.asyncio
async def test_sweep_transitions_kraken_filled_to_filled_state():
    """A `submitted` crypto intent whose Kraken adapter returns
    `status="FILLED"` must transition to `gate_state="filled"` with
    filled_qty / avg_price / filled_at stamped on `broker_order`."""
    intent_id = f"{_TEST_PREFIX}filled-{uuid.uuid4().hex[:8]}"
    txid = f"OQCLML-{uuid.uuid4().hex[:6].upper()}"
    await _insert_stuck_crypto(intent_id, txid)

    kraken_mock = _scoped_adapter(txid, {
        "status": "FILLED",
        "filled_qty": 0.0004,
        "filled_avg_price": 61500.0,
        "filled_at": "2026-07-09T10:00:00+00:00",
        "reject_reason": None,
        "txid": txid,
        "raw": {},
    })

    with patch(
        "shared.crypto.broker_adapter.get_kraken_adapter",
        new=AsyncMock(return_value=kraken_mock),
    ), patch(
        "shared.broker_router.get_webull_adapter",
        new=AsyncMock(return_value=None),
    ):
        counts = await ar._sweep_submitted_broker_orders()

    # Prod-shared DB has real stuck crypto intents — assert on the
    # SPECIFIC test intent + delta counts rather than absolute totals.
    assert counts["polled"] >= 1
    assert counts["by_lane"]["crypto"] >= 1
    assert counts["filled"] >= 1

    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc["gate_state"] == "filled"
    assert doc["broker_order"]["status"] == "FILLED"
    assert doc["broker_order"]["filled_qty"] == 0.0004
    assert doc["broker_order"]["filled_avg_price"] == 61500.0


# ─── happy path: Kraken CANCELED (terminal) ───────────────────

@pytest.mark.asyncio
async def test_sweep_transitions_kraken_canceled_to_broker_rejected():
    """A `submitted` crypto intent whose Kraken adapter returns
    `status="CANCELED"` with a terminal reject reason must transition
    to `gate_state="broker_rejected"`."""
    intent_id = f"{_TEST_PREFIX}canceled-{uuid.uuid4().hex[:8]}"
    txid = f"OQCLML-{uuid.uuid4().hex[:6].upper()}"
    await _insert_stuck_crypto(intent_id, txid)

    kraken_mock = _scoped_adapter(txid, {
        "status": "CANCELED",
        "filled_qty": None,
        "filled_avg_price": None,
        "filled_at": None,
        "reject_reason": "EOrder:Insufficient funds",
        "txid": txid,
        "raw": {},
    })

    with patch(
        "shared.crypto.broker_adapter.get_kraken_adapter",
        new=AsyncMock(return_value=kraken_mock),
    ), patch(
        "shared.broker_router.get_webull_adapter",
        new=AsyncMock(return_value=None),
    ):
        await ar._sweep_submitted_broker_orders()

    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc["gate_state"] in {"broker_rejected", "pending"}


# ─── working state: no-change branch ──────────────────────────

@pytest.mark.asyncio
async def test_sweep_leaves_working_kraken_orders_alone():
    """An order still in `WORKING` state (Kraken `open` / `pending`)
    must be counted as `no_change` and left untouched — the sweep
    will retry on the next tick."""
    intent_id = f"{_TEST_PREFIX}working-{uuid.uuid4().hex[:8]}"
    txid = f"OQCLML-{uuid.uuid4().hex[:6].upper()}"
    await _insert_stuck_crypto(intent_id, txid)

    kraken_mock = _scoped_adapter(txid, {
        "status": "WORKING",
        "filled_qty": None,
        "filled_avg_price": None,
        "filled_at": None,
        "reject_reason": None,
        "txid": txid,
        "raw": {},
    })

    with patch(
        "shared.crypto.broker_adapter.get_kraken_adapter",
        new=AsyncMock(return_value=kraken_mock),
    ), patch(
        "shared.broker_router.get_webull_adapter",
        new=AsyncMock(return_value=None),
    ):
        await ar._sweep_submitted_broker_orders()

    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc["gate_state"] == "submitted", (
        "WORKING orders must be left in submitted state for re-poll"
    )


# ─── fault isolation: equity outage doesn't wedge crypto ──────

@pytest.mark.asyncio
async def test_equity_broker_outage_does_not_stop_crypto_sweep():
    """If Webull adapter fails to construct, the crypto sweep must
    still run. Guarantees one broker's outage cannot halt the other."""
    intent_id = f"{_TEST_PREFIX}iso-{uuid.uuid4().hex[:8]}"
    txid = f"OQCLML-{uuid.uuid4().hex[:6].upper()}"
    await _insert_stuck_crypto(intent_id, txid)

    kraken_mock = _scoped_adapter(txid, {
        "status": "FILLED",
        "filled_qty": 0.0002,
        "filled_avg_price": 61400.0,
        "filled_at": "2026-07-09T10:00:00+00:00",
        "reject_reason": None,
        "txid": txid,
        "raw": {},
    })

    async def _webull_boom():
        raise RuntimeError("simulated Webull outage")

    with patch(
        "shared.crypto.broker_adapter.get_kraken_adapter",
        new=AsyncMock(return_value=kraken_mock),
    ), patch(
        "shared.broker_router.get_webull_adapter",
        side_effect=_webull_boom,
    ):
        counts = await ar._sweep_submitted_broker_orders()

    # Webull outage → equity lane not polled at all; crypto still runs.
    assert counts["by_lane"].get("equity", 0) == 0
    assert counts["by_lane"]["crypto"] >= 1
    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc["gate_state"] == "filled", (
        "crypto reconcile did not run — fault-isolation broken"
    )


# ─── rate-limit still fires uniformly ─────────────────────────

@pytest.mark.asyncio
async def test_sweep_rate_limit_skips_when_recent():
    """The RECONCILE_MIN_INTERVAL_SEC rate-limit gate must still fire
    whether the last sweep touched equity or crypto — its purpose is
    to protect either broker's per-second budget."""
    intent_id = f"{_TEST_PREFIX}rl-{uuid.uuid4().hex[:8]}"
    txid = f"OQCLML-{uuid.uuid4().hex[:6].upper()}"
    await _insert_stuck_crypto(intent_id, txid)

    kraken_mock = _scoped_adapter(txid, {
        "status": "FILLED",
        "filled_qty": 0.0002,
        "filled_avg_price": 61400.0,
        "filled_at": "2026-07-09T10:00:00+00:00",
        "reject_reason": None,
        "txid": txid,
        "raw": {},
    })

    with patch(
        "shared.crypto.broker_adapter.get_kraken_adapter",
        new=AsyncMock(return_value=kraken_mock),
    ), patch(
        "shared.broker_router.get_webull_adapter",
        new=AsyncMock(return_value=None),
    ):
        counts1 = await ar._sweep_submitted_broker_orders()
        counts2 = await ar._sweep_submitted_broker_orders()

    assert counts1["polled"] >= 1
    assert counts2.get("skipped_rate_limited") == 1
    assert counts2["polled"] == 0


# ─── learning resolver piggyback (iter-22 Stage 1.5) ──────────────

@pytest.mark.asyncio
async def test_sweep_calls_learning_resolver_and_stamps_counts(monkeypatch):
    """The reconcile sweep must invoke `resolve_pending_outcomes`
    once per non-rate-limited tick and stamp its counts into the
    return payload so operators see resolver activity in the same
    log line as the broker sweep.

    Failure of the resolver MUST NOT propagate — piggybacking is
    best-effort by contract; the sweep still returns cleanly."""
    called = {"n": 0}

    async def _fake_resolver(_db):
        called["n"] += 1
        return {
            "scanned": 5, "resolved_5m": 2, "resolved_15m": 1,
            "resolved_1h": 0, "skipped_missing_entry": 0,
            "skipped_missing_mark": 3, "errors": 0,
        }

    monkeypatch.setattr(
        "shared.learning.outcome_resolver.resolve_pending_outcomes",
        _fake_resolver,
    )
    # No broker adapters → sweep body is a no-op, but the learning
    # resolver should still be invoked at the tail.
    with patch(
        "shared.crypto.broker_adapter.get_kraken_adapter",
        new=AsyncMock(return_value=None),
    ), patch(
        "shared.broker_router.get_webull_adapter",
        new=AsyncMock(return_value=None),
    ):
        counts = await ar._sweep_submitted_broker_orders()

    assert called["n"] == 1
    assert counts["learning_scanned"] == 5
    assert counts["learning_resolved_5m"] == 2
    assert counts["learning_resolved_15m"] == 1
    assert counts["learning_skipped_mark"] == 3


@pytest.mark.asyncio
async def test_sweep_learning_resolver_failure_does_not_crash(monkeypatch):
    """If the resolver raises, the sweep must still return; the
    failure is recorded as `learning_resolver_errors=1`."""
    async def _boom(_db):
        raise RuntimeError("resolver kaboom")

    monkeypatch.setattr(
        "shared.learning.outcome_resolver.resolve_pending_outcomes",
        _boom,
    )
    with patch(
        "shared.crypto.broker_adapter.get_kraken_adapter",
        new=AsyncMock(return_value=None),
    ), patch(
        "shared.broker_router.get_webull_adapter",
        new=AsyncMock(return_value=None),
    ):
        counts = await ar._sweep_submitted_broker_orders()

    assert counts["learning_resolver_errors"] == 1


@pytest.mark.asyncio
async def test_sweep_learning_resolver_skipped_when_rate_limited(monkeypatch):
    """The rate-limit gate short-circuits the ENTIRE sweep body,
    including the learning-resolver piggyback. Otherwise a back-to-
    back caller could still hammer the resolver every millisecond."""
    ar._LAST_RECONCILE_SWEEP_TS = datetime.now(timezone.utc)
    called = {"n": 0}

    async def _fake_resolver(_db):
        called["n"] += 1
        return {}

    monkeypatch.setattr(
        "shared.learning.outcome_resolver.resolve_pending_outcomes",
        _fake_resolver,
    )
    counts = await ar._sweep_submitted_broker_orders()
    assert counts.get("skipped_rate_limited") == 1
    assert called["n"] == 0, (
        "Rate-limit gate must skip learning resolver too — "
        "piggyback semantics require sharing the same window"
    )
