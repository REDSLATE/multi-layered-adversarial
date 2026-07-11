"""Unit tests for the 3-clock intent-write health tracking
(2026-02-20 operator directive).

Scope:
    * `bump_stack_decision`, `bump_stack_intent_attempt`,
      `bump_stack_intent_success`, `bump_stack_intent_failure`
      write the expected fields / counters.
    * A directional (BUY/SELL/SHORT/COVER) success stamps
      `last_db_confirmed_directional_intent_ts`; a HOLD does not.
    * `bump_stack_intent_failure` records the error string and
      bumps `intent_submit_failures_total` — the CALLER is
      responsible for re-raising (verified by the intents-path
      integration test below).
    * `_write_health_band` returns the expected band across the
      HEALTHY / STALE / DEAD / UNKNOWN / BLIND matrix.
    * The stack-status endpoint decorates each brain section with
      `_ages` + `write_health`.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    try:
        with open("/app/frontend/.env") as _f:
            for _ln in _f:
                if _ln.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = _ln.split("=", 1)[1].strip().rstrip("/")
                    break
    except Exception:
        pass

ADMIN_EMAIL = "admin@risedual.io"
ADMIN_PASSWORD = "risedual-admin-2026"


@pytest.fixture(scope="module")
def admin_token() -> str:
    assert BASE_URL, "REACT_APP_BACKEND_URL not configured"
    resp = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def auth_headers(admin_token: str) -> dict:
    return {"Authorization": f"Bearer {admin_token}"}


# ────────────────────── band matrix (pure) ──────────────────────

def test_write_health_band_matrix():
    from routes.brain_runtime import _write_health_band

    # heartbeat stale → BLIND regardless of db_age
    assert _write_health_band(
        heartbeat_age_s=999, db_write_age_s=1, equity_open=True,
    ) == "BLIND"
    assert _write_health_band(
        heartbeat_age_s=None, db_write_age_s=1, equity_open=True,
    ) == "BLIND"
    # heartbeat fresh but no write ever → UNKNOWN
    assert _write_health_band(
        heartbeat_age_s=10, db_write_age_s=None, equity_open=True,
    ) == "UNKNOWN"
    # HEALTHY <15m
    assert _write_health_band(
        heartbeat_age_s=5, db_write_age_s=60, equity_open=True,
    ) == "HEALTHY"
    # STALE 15-60m
    assert _write_health_band(
        heartbeat_age_s=5, db_write_age_s=30 * 60, equity_open=True,
    ) == "STALE"
    # DEAD >60m during equity session
    assert _write_health_band(
        heartbeat_age_s=5, db_write_age_s=90 * 60, equity_open=True,
    ) == "DEAD"
    # Off-hours: >60m stays STALE until the 12h relax threshold
    assert _write_health_band(
        heartbeat_age_s=5, db_write_age_s=2 * 3600, equity_open=False,
    ) == "STALE"
    # Off-hours: crossing 12h flips to DEAD
    assert _write_health_band(
        heartbeat_age_s=5, db_write_age_s=13 * 3600, equity_open=False,
    ) == "DEAD"


# ────────────────────── bump helpers (async) ──────────────────────

@pytest.mark.asyncio
async def test_bump_stack_decision_sets_ts_and_counter():
    from shared.brain_runtime_metrics import bump_stack_decision
    from db import db
    from shared.brain_runtime_metrics import COLLECTION

    brain = f"__test_dec_{int(time.time())}"
    await bump_stack_decision(brain=brain, action="BUY", symbol="AAPL")
    doc = await db[COLLECTION].find_one({"_id": "risedual_stack"})
    section = (doc or {}).get("brains", {}).get(brain) or {}
    assert section.get("last_decision_ts") is not None
    assert section.get("last_decision_action") == "BUY"
    assert section.get("last_decision_symbol") == "AAPL"
    assert section.get("decisions_total") == 1
    # second bump increments counter
    await bump_stack_decision(brain=brain, action="HOLD", symbol="TSLA")
    doc = await db[COLLECTION].find_one({"_id": "risedual_stack"})
    section = (doc or {}).get("brains", {}).get(brain) or {}
    assert section.get("decisions_total") == 2
    assert section.get("last_decision_action") == "HOLD"
    # cleanup
    await db[COLLECTION].update_one(
        {"_id": "risedual_stack"},
        {"$unset": {f"brains.{brain}": ""}},
    )


@pytest.mark.asyncio
async def test_bump_stack_intent_success_directional_vs_hold():
    from shared.brain_runtime_metrics import bump_stack_intent_success
    from db import db
    from shared.brain_runtime_metrics import COLLECTION

    brain = f"__test_succ_{int(time.time())}"
    # HOLD → any-intent stamp but no directional stamp
    await bump_stack_intent_success(
        brain=brain, intent_id="i-hold-1", mongo_id="m-1",
        action="HOLD", symbol="SPY", lane="equity",
        ingest_ts="2026-02-20T12:00:00+00:00",
    )
    doc = await db[COLLECTION].find_one({"_id": "risedual_stack"})
    section = (doc or {}).get("brains", {}).get(brain) or {}
    assert section.get("last_db_confirmed_intent_ts") == "2026-02-20T12:00:00+00:00"
    assert section.get("last_db_confirmed_directional_intent_ts") is None
    assert section.get("intent_submit_successes_total") == 1
    assert section.get("directional_submit_successes_total") is None or \
        section.get("directional_submit_successes_total") == 0

    # BUY → both stamps
    await bump_stack_intent_success(
        brain=brain, intent_id="i-buy-1", mongo_id="m-2",
        action="BUY", symbol="NVDA", lane="equity",
        ingest_ts="2026-02-20T12:05:00+00:00",
    )
    doc = await db[COLLECTION].find_one({"_id": "risedual_stack"})
    section = (doc or {}).get("brains", {}).get(brain) or {}
    assert section.get("last_db_confirmed_intent_ts") == "2026-02-20T12:05:00+00:00"
    assert section.get("last_db_confirmed_directional_intent_ts") == "2026-02-20T12:05:00+00:00"
    assert section.get("intent_submit_successes_total") == 2
    assert section.get("directional_submit_successes_total") == 1
    receipt = section.get("last_write_receipt") or {}
    assert receipt.get("intent_id") == "i-buy-1"
    assert receipt.get("action") == "BUY"

    # cleanup
    await db[COLLECTION].update_one(
        {"_id": "risedual_stack"},
        {"$unset": {f"brains.{brain}": ""}},
    )


@pytest.mark.asyncio
async def test_bump_stack_intent_failure_records_error():
    from shared.brain_runtime_metrics import bump_stack_intent_failure
    from db import db
    from shared.brain_runtime_metrics import COLLECTION

    brain = f"__test_fail_{int(time.time())}"
    await bump_stack_intent_failure(
        brain=brain,
        error="NetworkTimeout: connection to atlas failed",
        action="SELL",
        symbol="MSFT",
    )
    doc = await db[COLLECTION].find_one({"_id": "risedual_stack"})
    section = (doc or {}).get("brains", {}).get(brain) or {}
    assert section.get("intent_submit_failures_total") == 1
    assert "NetworkTimeout" in (section.get("last_intent_submit_error_msg") or "")
    assert section.get("last_intent_submit_error_action") == "SELL"
    assert section.get("last_intent_submit_error_symbol") == "MSFT"
    # cleanup
    await db[COLLECTION].update_one(
        {"_id": "risedual_stack"},
        {"$unset": {f"brains.{brain}": ""}},
    )


# ────────────────────── stack/status decoration ──────────────────────

def test_stack_status_decorates_write_health(auth_headers):
    """The `/admin/runtime/stack/status` endpoint attaches `_ages`
    and `write_health` to every brain section."""
    resp = requests.get(
        f"{BASE_URL}/api/admin/runtime/stack/status",
        headers=auth_headers,
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("ok") is True
    assert "equity_market_open" in body
    brains = body.get("brains") or {}
    # at least one brain must be present after normal system activity
    if not brains:
        pytest.skip("no brains have emitted yet — nothing to decorate")
    for name, section in brains.items():
        assert "_ages" in section, f"brain {name} missing _ages"
        assert "write_health" in section, f"brain {name} missing write_health"
        band = section["write_health"]
        assert band in {"HEALTHY", "STALE", "DEAD", "UNKNOWN", "BLIND"}


# ────────────────────── write-failure propagation ──────────────────────

@pytest.mark.asyncio
async def test_insert_failure_propagates_and_bumps_failure_counter(monkeypatch):
    """When `shared_intents.insert_one` raises, the intents path
    must (a) bump `intent_submit_failures_total`, (b) re-raise so
    the runner sees the failure.

    Wiring note: motor's `AsyncIOMotorDatabase.__getitem__` returns
    a FRESH `AsyncIOMotorCollection` wrapper on every access — so
    patching `insert_one` on a wrapper captured up-front does not
    affect the wrapper the production code acquires at runtime.
    We patch the class method itself with a name-guarded shim so
    only the `shared_intents` collection raises; every other
    collection continues to work normally (metrics bumps still
    reach `brain_runtime_metrics`).
    """
    from motor.motor_asyncio import AsyncIOMotorCollection
    from shared import intents as intents_mod
    from shared.brain_runtime_metrics import COLLECTION
    from shared.brain_legend import canonicalize_stack
    from db import db
    from namespaces import SHARED_INTENTS

    brain = "camino"
    canon = canonicalize_stack(brain) or brain
    doc0 = await db[COLLECTION].find_one({"_id": "risedual_stack"}) or {}
    before = ((doc0.get("brains") or {}).get(canon) or {}).get(
        "intent_submit_failures_total", 0,
    ) or 0

    original_insert_one = AsyncIOMotorCollection.insert_one

    async def _cond_raise(self, doc, *args, **kwargs):
        if getattr(self, "name", None) == SHARED_INTENTS:
            raise RuntimeError("simulated_atlas_outage")
        return await original_insert_one(self, doc, *args, **kwargs)

    monkeypatch.setattr(AsyncIOMotorCollection, "insert_one", _cond_raise)

    body = intents_mod.IntentIn(
        stack=brain,
        action="HOLD",
        symbol="AAPL",
        lane="equity",
        confidence=0.5,
        rationale="write-health regression test",
    )
    with pytest.raises(RuntimeError, match="simulated_atlas_outage"):
        await intents_mod._post_intent_impl(body)

    doc1 = await db[COLLECTION].find_one({"_id": "risedual_stack"}) or {}
    after = ((doc1.get("brains") or {}).get(canon) or {}).get(
        "intent_submit_failures_total", 0,
    ) or 0
    assert after == before + 1
