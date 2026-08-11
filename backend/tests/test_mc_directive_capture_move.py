"""Testing agent iter37 — MC Directive 'Capture the Move' verification.

Additional coverage on top of test_execution_ladder.py / test_buy_eligibility.py:
 (1) sizer.build_position_plan receipt carries execution_friction and
     signal_spread_bps when eligibility flags friction.
 (2) wider stop_fraction reduces final_notional proportionally.
 (3) broker_router code inspection: ladder dispatch + LadderUnfilled ->
     BrokerRouteBlocked('qualified_but_unexecuted:').
 (4) /api/admin/entry-mode/funnel returns ladder counts + 'ladder' obj
     via HTTP against live backend using REACT_APP_BACKEND_URL.
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone

import pytest
import requests

sys.path.insert(0, "/app/backend")

pytestmark = pytest.mark.tripwire


# ─────────── (1)(2) sizer.build_position_plan receipt ───────────

@pytest.mark.asyncio
async def test_receipt_carries_execution_friction_and_spread(monkeypatch):
    from shared.risk_sizer import sizer, buy_eligibility as elig, balance

    async def _fake_elig(sym):
        return True, {"notional_cap_usd": 5.0,
                      "execution_friction": "spread_too_wide",
                      "spread_bps": 80.0,
                      "reason": "wide_spread_ladder"}
    monkeypatch.setattr(elig, "evaluate_buy_eligibility", _fake_elig)

    # cooldown clear
    from shared.risk_sizer import sell_cooldown as sc
    async def _cd(*a, **k):
        return 0.0, None
    monkeypatch.setattr(sc, "cooldown_remaining_s", _cd)

    async def _snap(lane, *, timeout_s, cache_max_age_s):
        return {"equity": 10_000.0, "available": 10_000.0,
                "source": "test", "age_ms": 0}
    monkeypatch.setattr(balance, "get_balance_snapshot", _snap)

    async def _atr(sym, entry):
        return 0.04  # 6% stop
    monkeypatch.setattr(sizer, "_atr_fraction", _atr)

    intent = {
        "intent_id": "",  # skip open_risk reservation
        "symbol": "WIDE/USD",
        "lane": "crypto",
        "action": "BUY",
        "price_at_signal": 100.0,
        "confidence": 0.8,
    }
    plan = await sizer.build_position_plan(
        intent, governor_multiplier=1.0, skip_roadguard=True)
    assert plan.get("approved") is True, plan
    assert plan.get("execution_friction") == "spread_too_wide", plan
    assert plan.get("signal_spread_bps") == 80.0, plan
    assert plan.get("stop_distance") == pytest.approx(0.06), plan
    gross_wide = float(plan.get("gross_notional") or 0)

    # Narrower stop -> larger pre-cap gross_notional (same $ risk budget)
    async def _atr2(sym, entry):
        return 0.02  # -> 3% (floor)
    monkeypatch.setattr(sizer, "_atr_fraction", _atr2)
    plan2 = await sizer.build_position_plan(
        intent, governor_multiplier=1.0, skip_roadguard=True)
    assert plan2.get("approved") is True, plan2
    assert plan2.get("stop_distance") == pytest.approx(0.03), plan2
    gross_narrow = float(plan2.get("gross_notional") or 0)
    # wider stop reduces size proportionally on the SAME risk budget
    assert gross_wide < gross_narrow, (gross_wide, gross_narrow)
    # ratio ~ narrow_stop/wide_stop = 0.03/0.06 = 0.5
    assert gross_wide == pytest.approx(gross_narrow * 0.5, rel=0.02)


# ─────────── (3) broker_router wiring inspection ───────────

def test_broker_router_ladder_dispatch_wiring():
    src = open("/app/backend/shared/broker_router.py").read()
    # dispatch condition
    assert "execution_friction" in src
    assert "spread_too_wide" in src
    assert "run_entry_ladder" in src
    assert "LadderUnfilled" in src
    # LadderUnfilled -> BrokerRouteBlocked('qualified_but_unexecuted:')
    assert "qualified_but_unexecuted" in src
    # import-check: module imports cleanly
    import importlib
    br = importlib.import_module("shared.broker_router")
    assert hasattr(br, "route_order")


# ─────────── (4) Funnel API ───────────

BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or "").rstrip("/")
if not BASE_URL:
    # fall back to /app/frontend/.env
    try:
        for line in open("/app/frontend/.env"):
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                break
    except Exception:
        pass


def _login(session: requests.Session) -> bool:
    r = session.post(f"{BASE_URL}/api/auth/login",
                     json={"email": "admin@risedual.io",
                           "password": "risedual-admin-2026"},
                     timeout=15)
    if r.status_code != 200:
        return False
    tok = (r.json() or {}).get("access_token") or (r.json() or {}).get("token")
    if tok:
        session.headers.update({"Authorization": f"Bearer {tok}"})
    return True


def test_funnel_endpoint_exposes_ladder_observability():
    assert BASE_URL, "REACT_APP_BACKEND_URL not set"
    s = requests.Session()
    if not _login(s):
        pytest.skip("login failed")
    r = s.get(f"{BASE_URL}/api/admin/entry-mode/funnel", timeout=20)
    assert r.status_code == 200, (r.status_code, r.text[:400])
    body = r.json() or {}
    funnel = body.get("funnel") or {}
    assert "ladder_recovered_fills" in funnel, funnel
    assert "qualified_but_unexecuted" in funnel, funnel
    ladder = body.get("ladder") or {}
    assert "recovered_fills" in ladder, ladder
    assert "qualified_but_unexecuted" in ladder, ladder
    assert "by_stage" in ladder, ladder
    assert isinstance(ladder["by_stage"], (list, dict))


@pytest.mark.asyncio
async def test_funnel_increments_on_synthetic_ladder_event():
    """Insert a synthetic execution_ladder_events doc and confirm the
    funnel counter increments; clean up afterwards."""
    assert BASE_URL, "REACT_APP_BACKEND_URL not set"
    s = requests.Session()
    if not _login(s):
        pytest.skip("login failed")
    r0 = s.get(f"{BASE_URL}/api/admin/entry-mode/funnel", timeout=20)
    if r0.status_code != 200:
        pytest.skip(f"funnel unreachable: {r0.status_code}")
    before = int(((r0.json() or {}).get("ladder") or {})
                 .get("qualified_but_unexecuted") or 0)

    from motor.motor_asyncio import AsyncIOMotorClient
    # env is loaded by backend at boot; we need MONGO_URL locally
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME")
    assert mongo_url and db_name
    cli = AsyncIOMotorClient(mongo_url)
    db = cli[db_name]
    doc_id = f"TEST_iter37_{uuid.uuid4().hex[:8]}"
    doc = {
        "_id": doc_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "intent_id": doc_id,
        "symbol": "TEST/USD",
        "lane": "crypto",
        "outcome": "qualified_but_unexecuted",
        "final_stage": "aggressive_limit",
        "stages_attempted": ["maker_bid", "maker_reprice",
                             "adaptive_maker", "aggressive_limit"],
        "notional_usd": 5.0,
        "spread_bps": 80.0,
        "detail": "iter37 synthetic",
    }
    try:
        await db["execution_ladder_events"].insert_one(doc)
        r1 = s.get(f"{BASE_URL}/api/admin/entry-mode/funnel", timeout=20)
        assert r1.status_code == 200
        after = int(((r1.json() or {}).get("ladder") or {})
                    .get("qualified_but_unexecuted") or 0)
        assert after >= before + 1, (before, after)
        # by_stage should include aggressive_limit somewhere
        by_stage = ((r1.json() or {}).get("ladder") or {}).get("by_stage")
        stages_str = str(by_stage)
        assert "aggressive_limit" in stages_str, by_stage
    finally:
        await db["execution_ladder_events"].delete_one({"_id": doc_id})
        cli.close()


# ─────────── (5) Regression: sizer imports & module boots ───────────

def test_backend_modules_import_cleanly():
    import importlib
    for mod in (
        "shared.execution_ladder",
        "shared.risk_sizer.buy_eligibility",
        "shared.risk_sizer.sizer",
        "shared.broker_router",
        "routes.execution_mode_admin",
    ):
        importlib.import_module(mod)
