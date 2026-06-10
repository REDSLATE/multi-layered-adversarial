"""Regression tests for `/api/admin/runtime/{brain}/intent-summary`.

Doctrine pin (2026-06-10, P2): operator situational awareness —
answers "what has Camaro been doing for the last hour?" without
opening Mongo.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import requests  # noqa: E402

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    # fallback for in-cluster
    BASE_URL = "http://localhost:8001"


def _get_token() -> str:
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={
            "email": "admin@risedual.io",
            "password": "risedual-admin-2026",
        },
        timeout=10,
    )
    r.raise_for_status()
    body = r.json()
    return body.get("access_token") or body.get("token")


@pytest.fixture(scope="module")
def _token():
    return _get_token()


def _seed_intents(brain: str, rows: list[dict]):
    """Drop test intents into `shared_intents` using sync pymongo so
    the route's async motor reads them."""
    import pymongo
    c = pymongo.MongoClient(os.environ["MONGO_URL"])
    db = c[os.environ["DB_NAME"]]
    db.shared_intents.insert_many(rows)
    c.close()


def _wipe_test_intents(brain: str):
    import pymongo
    c = pymongo.MongoClient(os.environ["MONGO_URL"])
    db = c[os.environ["DB_NAME"]]
    db.shared_intents.delete_many({
        "intent_id": {"$regex": "^test-intent-summary-"}
    })
    c.close()


@pytest.fixture(autouse=True)
def _clean_test_rows():
    _wipe_test_intents("camaro_test")
    yield
    _wipe_test_intents("camaro_test")


def _intent_row(brain, action, symbol, lane="equity", gate_state="passed", n=0):
    now = datetime.now(timezone.utc)
    return {
        "intent_id": f"test-intent-summary-{n}",
        "stack": brain,
        "action": action,
        "symbol": symbol,
        "lane": lane,
        "confidence": 0.7,
        "gate_state": gate_state,
        "ingest_ts": now.isoformat().replace("+00:00", "Z"),
    }


def test_summary_returns_aggregates(_token):
    """Seed 5 intents and verify counts come back correctly."""
    brain = "camaro_test"
    _seed_intents(brain, [
        _intent_row(brain, "BUY", "AAPL", n=1),
        _intent_row(brain, "BUY", "AAPL", n=2),
        _intent_row(brain, "SELL", "TSLA", n=3),
        _intent_row(brain, "HOLD", "NVDA", gate_state="pending", n=4),
        _intent_row(brain, "HOLD", "NVDA", lane="crypto", gate_state="pending", n=5),
    ])
    r = requests.get(
        f"{BASE_URL}/api/admin/runtime/{brain}/intent-summary",
        params={"minutes": 60, "limit": 10},
        headers={"Authorization": f"Bearer {_token}"},
        timeout=10,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["brain"] == brain
    assert body["total_intents"] == 5
    assert body["by_action"] == {"BUY": 2, "SELL": 1, "HOLD": 2}
    assert body["by_lane"] == {"equity": 4, "crypto": 1}
    assert body["by_verdict"] == {"passed": 3, "pending": 2}
    by_symbol = {row["symbol"]: row["count"] for row in body["by_symbol"]}
    assert by_symbol == {"AAPL": 2, "TSLA": 1, "NVDA": 2}
    assert len(body["recent"]) == 5
    assert body["last_emitted_at"] is not None


def test_summary_empty_brain(_token):
    """Brain with no intents → all zeros, no error."""
    r = requests.get(
        f"{BASE_URL}/api/admin/runtime/nonexistent_brain_xyz/intent-summary",
        headers={"Authorization": f"Bearer {_token}"},
        timeout=10,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_intents"] == 0
    assert body["by_action"] == {}
    assert body["by_lane"] == {}
    assert body["recent"] == []
    assert body["last_emitted_at"] is None


def test_summary_window_filters(_token):
    """Intents older than the window are excluded."""
    brain = "camaro_test"
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    fresh_ts = now.isoformat().replace("+00:00", "Z")
    _seed_intents(brain, [
        {**_intent_row(brain, "BUY", "AAPL", n=10),
         "ingest_ts": old_ts},
        {**_intent_row(brain, "BUY", "AAPL", n=11),
         "ingest_ts": fresh_ts},
    ])
    # 60-min window → only the fresh one
    r = requests.get(
        f"{BASE_URL}/api/admin/runtime/{brain}/intent-summary",
        params={"minutes": 60},
        headers={"Authorization": f"Bearer {_token}"},
        timeout=10,
    )
    assert r.status_code == 200
    assert r.json()["total_intents"] == 1
    # 1-day window → both
    r2 = requests.get(
        f"{BASE_URL}/api/admin/runtime/{brain}/intent-summary",
        params={"minutes": 24 * 60},
        headers={"Authorization": f"Bearer {_token}"},
        timeout=10,
    )
    assert r2.status_code == 200
    assert r2.json()["total_intents"] == 2


def test_summary_requires_auth():
    r = requests.get(
        f"{BASE_URL}/api/admin/runtime/camaro/intent-summary", timeout=10,
    )
    assert r.status_code in (401, 403), (
        f"unauthenticated request must be rejected; got {r.status_code}"
    )


def test_summary_limit_caps_recent(_token):
    brain = "camaro_test"
    _seed_intents(brain, [
        _intent_row(brain, "BUY", "AAPL", n=i) for i in range(20, 30)
    ])
    r = requests.get(
        f"{BASE_URL}/api/admin/runtime/{brain}/intent-summary",
        params={"limit": 3},
        headers={"Authorization": f"Bearer {_token}"},
        timeout=10,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_intents"] == 10
    assert len(body["recent"]) == 3
