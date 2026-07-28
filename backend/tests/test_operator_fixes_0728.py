"""Operator fixes 2026-07-28: SELL inventory gate, broker-min bump,
universe spread filter.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.risk_sizer import balance, open_risk, selection
from shared.risk_sizer import policy as sizer_policy
from shared.risk_sizer import sell_cooldown
from shared.risk_sizer.sizer import build_position_plan
from shared.universe.refresher import _apply_spread_filter

POLICY = {
    "crypto": dict(sizer_policy.DEFAULTS["crypto"]),
    "equity": dict(sizer_policy.DEFAULTS["equity"]),
    "options": dict(sizer_policy.DEFAULTS["options"]),
    "selection": dict(sizer_policy.DEFAULTS["selection"]),
    "balance": dict(sizer_policy.DEFAULTS["balance"]),
    "enabled": {"crypto": True, "equity": True, "options": True},
}


# ── universe spread filter (fix #4) ─────────────────────────────────

def test_spread_filter_drops_wide_pairs():
    rows = [
        {"canonical_symbol": "BTC/USD", "spread_bps": 2.0},
        {"canonical_symbol": "GAIB/USD", "spread_bps": 180.0},
        {"canonical_symbol": "NEW/USD", "spread_bps": None},
    ]
    kept, dropped = _apply_spread_filter(rows, 60.0, min_keep=2)
    assert [r["canonical_symbol"] for r in kept] == ["BTC/USD", "NEW/USD"]
    assert dropped[0]["canonical_symbol"] == "GAIB/USD"
    assert "wide_spread" in dropped[0]["_drop_reason"]


def test_spread_filter_min_keep_floor_refills_tightest():
    rows = [{"canonical_symbol": f"S{i}/USD", "spread_bps": 100.0 + i}
            for i in range(20)]
    kept, dropped = _apply_spread_filter(rows, 60.0, min_keep=12)
    assert len(kept) == 12
    # refilled with the TIGHTEST spreads first
    assert {r["canonical_symbol"] for r in kept} == {f"S{i}/USD" for i in range(12)}
    assert all("_drop_reason" in r for r in dropped)


def test_spread_filter_pinned_exempt():
    rows = [{"canonical_symbol": "X/USD", "spread_bps": 500.0, "pinned": True}]
    kept, dropped = _apply_spread_filter(rows, 60.0)
    assert len(kept) == 1 and not dropped


# ── broker-min bump (fix #3) ────────────────────────────────────────

@pytest.fixture
def wired(monkeypatch):
    balance.reset_for_tests()
    open_risk.reset_for_tests()
    selection.reset_for_tests()
    sell_cooldown.reset_for_tests()

    async def fake_policy():
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in POLICY.items()}

    async def fake_snapshot(lane, **_kw):
        return {"equity": 4500.0, "available": 4500.0, "source": "LIVE", "age_ms": 0}

    async def fake_exit_policy():
        return {"crypto": {"enabled": True, "sl_pct": 3.0, "tp_pct": 8.0},
                "equity": {"enabled": True, "sl_pct": 3.0, "tp_pct": 6.0}}

    monkeypatch.setattr("shared.risk_sizer.policy.get_sizer_policy", fake_policy)
    monkeypatch.setattr("shared.risk_sizer.balance.get_balance_snapshot", fake_snapshot)
    monkeypatch.setattr("shared.exits.policy.get_policy", fake_exit_policy)
    monkeypatch.setattr("shared.risk_sizer.open_risk.open_plan_risk", lambda lane: 0.0)
    monkeypatch.setattr(
        "shared.hotpath.policy_snapshot.get",
        lambda: {"master_switch_enabled": True, "broker_freeze_reason": None},
    )
    yield monkeypatch
    balance.reset_for_tests()
    open_risk.reset_for_tests()
    sell_cooldown.reset_for_tests()


def _crypto_intent(**over):
    doc = {"intent_id": "cr-bump-1", "lane": "crypto", "symbol": "SOL/USD",
           "action": "BUY", "price_at_signal": 150.0}
    doc.update(over)
    return doc


async def test_governor_risk_down_bumps_to_broker_min(wired):
    # gm=0.33 → risk $4500*0.005*0.33 = $7.43; /3% stop = $247 — fine.
    # Force sub-min via tiny gm: 0.02 → risk $0.45 → notional $15?? no:
    # $0.45/0.03 = $15 > $5 min. Use gm small enough: 0.005 → $0.11 →
    # $3.75 notional < $5 min → bump to $5.
    plan = await build_position_plan(_crypto_intent(), governor_multiplier=0.005)
    assert plan["approved"] is True
    assert plan["final_notional"] == 5.0
    assert plan["min_notional_bump"] is True


async def test_bump_disabled_knob_rejects(wired):
    POLICY["crypto"]["bump_to_broker_min"] = False
    try:
        plan = await build_position_plan(
            _crypto_intent(), governor_multiplier=0.005,
        )
        assert plan["approved"] is False
        assert plan["reason"] == "below_minimum_order_notional"
    finally:
        POLICY["crypto"]["bump_to_broker_min"] = True


async def test_normal_sizes_not_bumped(wired):
    plan = await build_position_plan(_crypto_intent(), governor_multiplier=1.0)
    assert plan["approved"] is True
    assert plan["min_notional_bump"] is False
    assert plan["final_notional"] > 5.0


# ── entry-order TTL cancel (LCID stale-limit autopsy 2026-07-28) ────

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from db import db
from namespaces import SHARED_INTENTS
from shared import auto_router as ar
from shared import auto_router_reconciliation as ar_recon

_TTL_PREFIX = "ttl-test-"


@pytest.fixture(autouse=True)
async def _ttl_cleanup(monkeypatch):
    monkeypatch.setattr(ar_recon, "RECONCILE_BATCH_CAP", 500)
    ar_recon._LAST_RECONCILE_SWEEP_TS = None
    await db[SHARED_INTENTS].delete_many(
        {"intent_id": {"$regex": f"^{_TTL_PREFIX}"}})
    yield
    await db[SHARED_INTENTS].delete_many(
        {"intent_id": {"$regex": f"^{_TTL_PREFIX}"}})
    ar_recon._LAST_RECONCILE_SWEEP_TS = None


def _ts_minutes_ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


async def _insert_working(intent_id: str, txid: str, age_min: float) -> None:
    await db[SHARED_INTENTS].insert_one({
        "intent_id": intent_id,
        "lane": "crypto",
        "symbol": "XBTUSD",
        "action": "BUY",
        "stack": "gto",
        "gate_state": "submitted",
        "executed": True,
        "ingest_ts": _ts_minutes_ago(age_min),
        "executed_at": _ts_minutes_ago(age_min),
        "broker_order": {"order_id": txid, "broker": "kraken",
                         "status": "submitted"},
        "final_notional_usd": 25.0,
    })


def _working_adapter(txid: str, filled_qty: float = 0.0):
    mock = MagicMock()

    async def _get_order(oid: str):
        if str(oid) == str(txid):
            return {"status": "WORKING", "filled_qty": filled_qty,
                    "txid": txid, "raw": {}}
        raise RuntimeError(f"out-of-scope txid {oid!r}")

    cancelled: list[str] = []

    async def _cancel(oid: str):
        cancelled.append(str(oid))

    mock.get_order = _get_order
    mock.cancel_order = _cancel
    mock._cancelled = cancelled
    return mock


async def _run_sweep(kraken_mock):
    with patch(
        "shared.crypto.broker_adapter.get_kraken_adapter",
        new=AsyncMock(return_value=kraken_mock),
    ), patch(
        "shared.broker_router.get_webull_adapter",
        new=AsyncMock(return_value=None),
    ):
        return await ar._sweep_submitted_broker_orders()


async def test_working_order_past_ttl_is_cancelled():
    intent_id = f"{_TTL_PREFIX}stale-{uuid.uuid4().hex[:8]}"
    txid = f"TTL-{uuid.uuid4().hex[:6].upper()}"
    await _insert_working(intent_id, txid, age_min=ar_recon.ENTRY_ORDER_TTL_MIN + 5)
    mock = _working_adapter(txid)
    counts = await _run_sweep(mock)
    assert txid in mock._cancelled
    assert counts.get("ttl_cancelled", 0) >= 1
    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc["gate_state"] == "expired_unfilled"
    assert "entry_order_ttl_cancelled" in doc["broker_reason"]
    assert doc["broker_order"]["status"] == "CANCELLED_TTL"


async def test_working_order_within_ttl_left_alone():
    intent_id = f"{_TTL_PREFIX}fresh-{uuid.uuid4().hex[:8]}"
    txid = f"TTL-{uuid.uuid4().hex[:6].upper()}"
    await _insert_working(intent_id, txid, age_min=2)
    mock = _working_adapter(txid)
    await _run_sweep(mock)
    assert txid not in mock._cancelled
    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc["gate_state"] == "submitted"


async def test_partial_fill_past_ttl_cancels_remainder_marks_filled():
    intent_id = f"{_TTL_PREFIX}partial-{uuid.uuid4().hex[:8]}"
    txid = f"TTL-{uuid.uuid4().hex[:6].upper()}"
    await _insert_working(intent_id, txid, age_min=ar_recon.ENTRY_ORDER_TTL_MIN + 5)
    mock = _working_adapter(txid, filled_qty=0.5)
    await _run_sweep(mock)
    assert txid in mock._cancelled
    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc["gate_state"] == "filled"
    assert doc["broker_order"]["filled_qty"] == 0.5


# ── allowlist-only BUY universe + pending TTL (2026-07-28) ──────────

from shared.risk_sizer import buy_allowlist


async def test_buy_allowlist_blocks_off_list_symbol(wired, monkeypatch):
    async def fake_al():
        return {"enabled": True, "symbols": ["BTC/USD", "ETH/USD"]}
    monkeypatch.setattr(buy_allowlist, "get_allowlist", fake_al)
    plan = await build_position_plan(
        _crypto_intent(symbol="TREMP/USD"), governor_multiplier=1.0,
    )
    assert plan["approved"] is False
    assert plan["reason"] == "not_in_buy_allowlist"


async def test_buy_allowlist_passes_listed_symbol(wired, monkeypatch):
    async def fake_al():
        return {"enabled": True, "symbols": ["BTC/USD", "ETH/USD", "SOL/USD"]}
    monkeypatch.setattr(buy_allowlist, "get_allowlist", fake_al)
    plan = await build_position_plan(_crypto_intent(), governor_multiplier=1.0)
    assert plan.get("reason") != "not_in_buy_allowlist"
    assert plan["approved"] is True


async def test_buy_allowlist_disabled_allows_everything(wired, monkeypatch):
    async def fake_al():
        return {"enabled": False, "symbols": []}
    monkeypatch.setattr(buy_allowlist, "get_allowlist", fake_al)
    plan = await build_position_plan(
        _crypto_intent(symbol="TREMP/USD"), governor_multiplier=1.0,
    )
    assert plan.get("reason") != "not_in_buy_allowlist"


async def test_buy_allowlist_never_gates_sells(wired, monkeypatch):
    async def fake_al():
        return {"enabled": True, "symbols": ["BTC/USD"]}
    monkeypatch.setattr(buy_allowlist, "get_allowlist", fake_al)
    plan = await build_position_plan(
        _crypto_intent(symbol="TREMP/USD", action="SELL"),
        governor_multiplier=1.0,
    )
    assert plan.get("reason") != "not_in_buy_allowlist"


async def test_pending_ttl_expires_stale_unrouted():
    intent_id = f"{_TTL_PREFIX}pending-{uuid.uuid4().hex[:8]}"
    await db[SHARED_INTENTS].insert_one({
        "intent_id": intent_id,
        "lane": "crypto", "symbol": "TREMP/USD", "action": "SELL",
        "stack": "barracuda", "gate_state": "pending",
        "executed": False,
        "ingest_ts": _ts_minutes_ago(ar_recon.PENDING_INTENT_TTL_MIN + 10),
    })
    n = await ar_recon._sweep_stale_pending()
    assert n >= 1
    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc["gate_state"] == "expired_unrouted"
    assert doc["broker_reason"] == "EXPIRED_PENDING_TTL"


async def test_pending_ttl_leaves_fresh_pending_alone():
    intent_id = f"{_TTL_PREFIX}pendfresh-{uuid.uuid4().hex[:8]}"
    await db[SHARED_INTENTS].insert_one({
        "intent_id": intent_id,
        "lane": "crypto", "symbol": "BTC/USD", "action": "BUY",
        "stack": "gto", "gate_state": "pending",
        "executed": False,
        "ingest_ts": _ts_minutes_ago(2),
    })
    await ar_recon._sweep_stale_pending()
    doc = await db[SHARED_INTENTS].find_one({"intent_id": intent_id})
    assert doc["gate_state"] == "pending"
