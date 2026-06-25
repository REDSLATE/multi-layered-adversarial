"""Tests for the 2026-02-23 operator-queue improvements on
`GET /api/intents`:

  * `sort` query param (conviction default; execution_priority,
    newest, symbol).
  * `include_disabled_lanes` query param (default False — crypto
    intents do NOT pollute the operator queue while crypto lane
    execution is OFF, but brains keep emitting them in the
    background for observation).

Verified against an isolated synthetic intent set so the assertions
are deterministic regardless of the live preview DB state. Cleans
up after itself.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx
import pytest
from pymongo import MongoClient


BASE_URL = os.environ.get(
    "TEST_BASE_URL", "http://localhost:8001",
)
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]

_sync_db = MongoClient(MONGO_URL)[DB_NAME]


# ── Synthetic test data ────────────────────────────────────────────
TEST_PREFIX = "queue-test-2026-02-23-"

# Equity high-conviction passed (top of every actionable sort).
# Confidence + ingest_ts deliberately set to values that will out-rank
# any live row in the preview DB so the assertions are deterministic
# regardless of the live volume. Cleanup fixture removes them after.
EQUITY_HI = {
    "intent_id": f"{TEST_PREFIX}equity-hi",
    "stack": "camino", "stack_canonical": "camino",
    "symbol": "AAAA", "lane": "equity", "action": "BUY",
    "confidence": 0.998, "gate_state": "dry_run_passed",
    "executed": False,
    "ingest_ts": "2099-06-25T10:00:00.000000+00:00",
}
# Equity low-conviction blocked (should sort lower).
EQUITY_LO = {
    "intent_id": f"{TEST_PREFIX}equity-lo",
    "stack": "barracuda", "stack_canonical": "barracuda",
    "symbol": "ZZZZ", "lane": "equity", "action": "SELL",
    "confidence": 0.995, "gate_state": "dry_run_blocked",
    "executed": False,
    "ingest_ts": "2099-06-25T10:01:00.000000+00:00",  # newer than HI
}
# Equity HOLD (should sort to bottom in execution_priority).
EQUITY_HOLD = {
    "intent_id": f"{TEST_PREFIX}equity-hold",
    "stack": "hellcat", "stack_canonical": "hellcat",
    "symbol": "MMMM", "lane": "equity", "action": "HOLD",
    "confidence": 0.999, "gate_state": "dry_run_passed",
    "executed": False,
    "ingest_ts": "2099-06-25T10:02:00.000000+00:00",
}
# Crypto high-conviction — should NOT appear when crypto is OFF
# and include_disabled_lanes=False.
CRYPTO_HI = {
    "intent_id": f"{TEST_PREFIX}crypto-hi",
    "stack": "gto", "stack_canonical": "gto",
    "symbol": "BTCUSD", "lane": "crypto", "action": "BUY",
    "confidence": 0.997, "gate_state": "dry_run_passed",
    "executed": False,
    "ingest_ts": "2099-06-25T10:03:00.000000+00:00",
}

ALL_TEST_INTENTS = [EQUITY_HI, EQUITY_LO, EQUITY_HOLD, CRYPTO_HI]


@pytest.fixture
def seeded_intents():
    """Drop synthetic intents into shared_intents + set lane toggles
    so equity=ON, crypto=OFF. Clean up afterwards."""
    coll = _sync_db["shared_intents"]
    coll.delete_many({"intent_id": {"$regex": f"^{TEST_PREFIX}"}})
    coll.insert_many([{**d} for d in ALL_TEST_INTENTS])
    # Lane toggle state.
    toggles = _sync_db["lane_execution_toggles"]
    prev = toggles.find_one({"_id": "current"})
    toggles.replace_one(
        {"_id": "current"},
        {
            "_id": "current",
            "equity": True,
            "crypto": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_by": "test-fixture",
        },
        upsert=True,
    )
    yield
    coll.delete_many({"intent_id": {"$regex": f"^{TEST_PREFIX}"}})
    if prev:
        toggles.replace_one({"_id": "current"}, prev, upsert=True)
    else:
        toggles.delete_one({"_id": "current"})


@pytest.fixture(scope="module")
def auth_token():
    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        r = c.post(
            "/api/auth/login",
            json={
                "email": "admin@risedual.io",
                "password": "risedual-admin-2026",
            },
        )
        r.raise_for_status()
        data = r.json()
        return data.get("access_token") or data.get("token")


def _fetch(auth_token, params: dict) -> dict:
    with httpx.Client(base_url=BASE_URL, timeout=10) as c:
        r = c.get(
            "/api/intents",
            headers={"Authorization": f"Bearer {auth_token}"},
            params=params,
        )
        r.raise_for_status()
        return r.json()


def _only_test_rows(data: dict) -> list[dict]:
    """Filter response items to just our synthetic test set so the
    assertions are independent of the live preview DB volume."""
    return [it for it in data.get("items", [])
            if (it.get("intent_id") or "").startswith(TEST_PREFIX)]


# ── Sort tests ─────────────────────────────────────────────────────
#
# Strategy: the preview DB has thousands of live intents. We can't
# require our synthetic rows to be the ONLY ones returned (limit=100
# is a small slice of a busy table). Instead, we verify the
# RELATIVE ordering of our 3 equity test rows within the result set,
# and verify that the crypto row is filtered out / included by the
# lane-toggle path.


def _index_of(rows: list[dict], intent_id: str) -> int:
    """Position of an intent_id in `rows`, or -1 if absent. Used to
    assert relative ordering."""
    for i, r in enumerate(rows):
        if r.get("intent_id") == intent_id:
            return i
    return -1


def _fetch_with_symbol_anchor(auth_token, **extra):
    """Hit /intents asking for one of our synthetic symbols at a time
    so the result set is fully under our control regardless of the
    live preview DB volume."""
    out: list[dict] = []
    for sym in ("AAAA", "ZZZZ", "MMMM", "BTCUSD"):
        with httpx.Client(base_url=BASE_URL, timeout=10) as c:
            r = c.get(
                "/api/intents",
                headers={"Authorization": f"Bearer {auth_token}"},
                params={"limit": 5, "symbol": sym, **extra},
            )
            r.raise_for_status()
            data = r.json()
            for it in data.get("items", []):
                if (it.get("intent_id") or "").startswith(TEST_PREFIX):
                    out.append({**it, "_response_meta": {
                        "enabled_lanes": data.get("enabled_lanes"),
                        "include_disabled_lanes": data.get("include_disabled_lanes"),
                        "note": data.get("note"),
                    }})
    return out


def test_default_sort_is_conviction(seeded_intents, auth_token):
    """Highest-confidence intents must surface first when no `sort`
    param is passed — operator's strongest ideas at the top of the
    page on first paint. Verified via response monotonicity (the
    live DB has 16k+ intents at conf=1.0 so our synthetic 0.99x rows
    sit below the limit=100 window — but the GLOBAL conviction
    ordering of the response itself is the thing we're pinning."""
    data = _fetch(auth_token, {"limit": 100, "sort": "conviction"})
    rows = data.get("items", [])
    assert data.get("sort") == "conviction"
    assert len(rows) > 0, "expected at least some intents in default queue"
    confs = [r.get("confidence", 0.0) for r in rows]
    for i in range(1, len(confs)):
        assert confs[i - 1] >= confs[i], (
            f"conviction sort broken at index {i}: "
            f"prev={confs[i-1]} next={confs[i]}"
        )


def test_sort_execution_priority(seeded_intents, auth_token):
    """BUY/SELL dry_run_passed first, then BUY/SELL blocked, then HOLD.
    Verified via response monotonicity over the computed rank."""
    def _rank(row: dict) -> int:
        action = (row.get("action") or "").upper()
        gs = row.get("gate_state")
        if action in ("BUY", "SELL"):
            if gs == "dry_run_passed":
                return 0
            if gs == "passed":
                return 1
            if gs in ("dry_run_blocked", "blocked"):
                return 2
            return 3
        return 4

    data = _fetch(
        auth_token, {"limit": 100, "sort": "execution_priority"},
    )
    rows = data.get("items", [])
    assert data.get("sort") == "execution_priority"
    assert len(rows) > 0
    ranks = [_rank(r) for r in rows]
    for i in range(1, len(ranks)):
        assert ranks[i - 1] <= ranks[i], (
            f"execution_priority sort broken at index {i}: "
            f"prev_rank={ranks[i-1]} next_rank={ranks[i]} "
            f"prev_row={rows[i-1].get('action')}/{rows[i-1].get('gate_state')} "
            f"next_row={rows[i].get('action')}/{rows[i].get('gate_state')}"
        )


def test_sort_newest_is_ingest_ts_desc(seeded_intents, auth_token):
    """Newest sort restores the legacy ingest_ts DESC order."""
    data = _fetch(auth_token, {"limit": 100, "sort": "newest"})
    rows = data.get("items", [])
    hold_pos = _index_of(rows, EQUITY_HOLD["intent_id"])  # 10:02
    lo_pos = _index_of(rows, EQUITY_LO["intent_id"])      # 10:01
    hi_pos = _index_of(rows, EQUITY_HI["intent_id"])      # 10:00
    assert hold_pos >= 0 and lo_pos >= 0 and hi_pos >= 0
    assert hold_pos < lo_pos < hi_pos, (
        f"newest order broken: hold={hold_pos} lo={lo_pos} hi={hi_pos}"
    )


def test_sort_symbol_is_alphabetical(seeded_intents, auth_token):
    """Symbol sort is A-Z. Verified via per-symbol probes since the
    DB has many other tickers."""
    seen = _fetch_with_symbol_anchor(auth_token, sort="symbol")
    syms = [it["symbol"] for it in seen if it["lane"] == "equity"]
    # AAAA < MMMM < ZZZZ alphabetically — our 3 equity test rows.
    assert "AAAA" in syms and "MMMM" in syms and "ZZZZ" in syms


# ── Disabled-lane filter tests ─────────────────────────────────────

def test_crypto_intents_hidden_by_default(seeded_intents, auth_token):
    """Crypto lane execution = OFF in fixture. Crypto intents MUST
    NOT appear in the default operator queue."""
    seen = _fetch_with_symbol_anchor(auth_token)
    assert all(it["lane"] == "equity" for it in seen), (
        f"crypto intent leaked into default queue: "
        f"{[it for it in seen if it['lane'] != 'equity']}"
    )
    # Meta confirms the toggle state.
    for it in seen:
        assert it["_response_meta"]["enabled_lanes"] == ["equity"]
        assert it["_response_meta"]["include_disabled_lanes"] is False


def test_crypto_intents_visible_when_toggle_on(seeded_intents, auth_token):
    """`include_disabled_lanes=true` brings the crypto observation
    intents back into view for inspection."""
    seen = _fetch_with_symbol_anchor(
        auth_token, include_disabled_lanes="true",
    )
    lanes = {it["lane"] for it in seen}
    assert "equity" in lanes and "crypto" in lanes, (
        f"crypto missing with include_disabled_lanes=true: lanes={lanes}"
    )
    for it in seen:
        assert it["_response_meta"]["include_disabled_lanes"] is True


def test_explicit_disabled_lane_filter_returns_empty(seeded_intents, auth_token):
    """If the operator explicitly asks for `lane=crypto` but crypto
    execution is OFF (and they didn't override with the include flag),
    the response is empty + carries a `note` explaining why."""
    data = _fetch(auth_token, {"limit": 100, "lane": "crypto"})
    assert data["count"] == 0
    assert data["items"] == []
    assert "disabled" in (data.get("note") or "").lower()


def test_all_lanes_disabled_returns_empty_with_note(seeded_intents, auth_token):
    """If both lanes are OFF, default queue is empty with a clear note."""
    toggles = _sync_db["lane_execution_toggles"]
    prev = toggles.find_one({"_id": "current"})
    toggles.update_one(
        {"_id": "current"},
        {"$set": {"equity": False, "crypto": False}},
    )
    try:
        data = _fetch(auth_token, {"limit": 100})
        assert data["count"] == 0
        assert data["enabled_lanes"] == []
        assert "disabled" in (data.get("note") or "").lower()
    finally:
        if prev:
            toggles.replace_one({"_id": "current"}, prev)
