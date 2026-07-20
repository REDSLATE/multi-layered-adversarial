"""Iteration 29 — Soft Degradation Phase 2 validation.

Validates:
  1. `webull_notional_band` — DEFAULT floor is now 5.00 (was 1.00),
     Mongo override can only RAISE the floor (never lower it).
  2. `_gate_risk` in shared/auto_router_stages.py:
     * Applies `evidence.size_multiplier` from the arbiter on top of
       the seat multiplier — crypto lane $10 × seat 1.0 × arb 0.5 = $5
       goes to risk.check.
     * Equity intents whose post-multiplier notional falls below the
       Webull $5 floor are SIZED UP to $5 with `floor_sized_up=true`
       in the `sizing_degradation` provenance stamp.
     * `evidence.size_multiplier=0.0` short-circuits BEFORE risk.check
       with `verdict='advisory_only'`, `broker_reason='SIZED_TO_ZERO'`,
       `broker_error_bucket='conviction_sizing'`, and an executions row
       with `broker_status='sized_to_zero'`.
     * When RISEDUAL_CAP_PER_ORDER_USD < equity floor, an equity intent
       that needs size-up blocks with reason=
       `equity_floor_exceeds_per_order_cap`.
  3. Live API smoke: /api/admin/auto-router/status + force-tick still
     pass under the new stage.

Tests use direct in-proc invocation of `_gate_risk(ctx)` with mocked
seat/risk/executions modules — NO real broker submits.
"""
from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")


BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL") or "").rstrip("/")


# ═══════════════════════════════════════════════════════════════════
# 1. Unit — webull_notional_band floor semantics (5.00 + max-lock)
# ═══════════════════════════════════════════════════════════════════
class TestWebullNotionalBandFloor:
    def _clear_cache(self):
        # Fresh import each test so module-level cache is reset
        import shared.broker.webull_caps as wc
        wc._CACHED_FLOOR_OVERRIDE = None
        wc._CACHED_FLOOR_TS = 0.0
        return wc

    def test_default_min_notional_is_5(self, monkeypatch):
        monkeypatch.delenv("WEBULL_MIN_NOTIONAL_USD", raising=False)
        wc = self._clear_cache()
        assert wc.DEFAULT_MIN_NOTIONAL_USD == 5.00

    def test_band_none_returns_floor_5(self, monkeypatch):
        # Env explicitly set to 5.00 per backend/.env
        monkeypatch.setenv("WEBULL_MIN_NOTIONAL_USD", "5.00")
        wc = self._clear_cache()
        lo, hi, src = wc.webull_notional_band(None)
        assert lo == 5.00, f"expected floor 5.00, got {lo}"
        assert hi >= lo

    def test_mongo_override_below_5_does_not_lower_floor(self, monkeypatch):
        """A stale Mongo override of 1.00 must NOT reopen the sub-$5
        reject path. Floor stays at env/default = 5.00."""
        monkeypatch.setenv("WEBULL_MIN_NOTIONAL_USD", "5.00")
        wc = self._clear_cache()
        # Seed the cache with a below-5 override directly (mirrors what
        # refresh_webull_floor_cache() would populate).
        wc._CACHED_FLOOR_OVERRIDE = 1.00
        wc._CACHED_FLOOR_TS = time.time()
        try:
            lo, _hi, _src = wc.webull_notional_band(None)
            assert lo == 5.00, (
                f"stale Mongo override 1.00 lowered floor to {lo} — "
                "sub-$5 reject path is REOPEN, violates 2026-07-20 doctrine"
            )
        finally:
            wc._CACHED_FLOOR_OVERRIDE = None
            wc._CACHED_FLOOR_TS = 0.0

    def test_mongo_override_above_5_raises_floor(self, monkeypatch):
        """An override of $7 must raise the floor to $7."""
        monkeypatch.setenv("WEBULL_MIN_NOTIONAL_USD", "5.00")
        wc = self._clear_cache()
        wc._CACHED_FLOOR_OVERRIDE = 7.00
        wc._CACHED_FLOOR_TS = time.time()
        try:
            lo, _hi, _src = wc.webull_notional_band(None)
            assert lo == 7.00, f"override 7.00 not applied, got {lo}"
        finally:
            wc._CACHED_FLOOR_OVERRIDE = None
            wc._CACHED_FLOOR_TS = 0.0


# ═══════════════════════════════════════════════════════════════════
# 2. _gate_risk scaffold — direct in-proc test
# ═══════════════════════════════════════════════════════════════════

def _seat_fire(lane="equity", risk_multiplier=1.0):
    return SimpleNamespace(
        verdict="fire", reason="strategist_proposes", lane=lane,
        intent_brain="camino", strategist="camino", governor="hellcat",
        executor="camino", auditor="barracuda",
        angels={"strategist": "Raziel", "governor": "Nuriel",
                "executor": "Paschar", "auditor": "Sariel"},
        risk_multiplier=risk_multiplier,
    )


class _RiskOK:
    def __init__(self, notional_usd=10.0):
        self.ok = True
        self.notional_usd = notional_usd
        self.reason = "ok"
        self.cap_per_order_usd = 10.0
        self.cap_daily_usd = 1000.0
        self.spent_today_usd = 0.0


def _make_ctx(intent, sd, notional_raw, notional_source="brain_legacy"):
    """Build a `RouteContext` primed for `_gate_risk` entry."""
    from shared.auto_router_helpers import RouteContext
    ctx = RouteContext(intent=intent)
    ctx.finalize_inputs()
    ctx.notional_raw = notional_raw
    ctx.notional_source = notional_source
    ctx.sd = sd
    return ctx


def _install_mocks(risk_check=None, per_order_cap=1000.0,
                   market_open=True, ext_hours_on=False, floor_result=None):
    """Return an ExitStack containing all patches _gate_risk needs.

    Caller uses `with _install_mocks(...) as stack:` and captures the
    tuple `(updated_docs, executions_record_mock)` for assertions.
    """
    from contextlib import ExitStack
    from shared import auto_router as ar

    updated_docs: list[dict] = []
    coll = MagicMock()

    async def _update_one(query, update):
        updated_docs.append({"query": query, "update": update})

    coll.update_one = _update_one
    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(return_value=coll)

    executions_mod = MagicMock()
    executions_mod.record = AsyncMock(return_value="exec-row-id")

    risk_mod = MagicMock()
    if risk_check is None:
        async def default_check(_intent, *, notional_usd):
            return _RiskOK(notional_usd=notional_usd)
        risk_mod.check = default_check
    else:
        risk_mod.check = risk_check
    risk_mod.per_order_cap = MagicMock(return_value=per_order_cap)

    stack = ExitStack()
    import shared as _shared_pkg  # noqa: WPS433
    stack.enter_context(patch.object(_shared_pkg, "risk", risk_mod, create=True))
    stack.enter_context(patch.object(_shared_pkg, "executions", executions_mod, create=True))
    stack.enter_context(patch.dict(sys.modules, {
        "shared.risk": risk_mod,
        "shared.executions": executions_mod,
    }))
    stack.enter_context(patch.object(ar, "db", fake_db))

    async def fake_apply_floor(pair, notional):
        if floor_result is not None:
            return floor_result
        return SimpleNamespace(
            allowed=True, notional_usd=notional, reject_reason=None,
            adjusted=False, original_notional=notional,
            floor=SimpleNamespace(pair=pair, min_notional_usd=0.0,
                                  policy="size_up", is_default=True),
        )
    stack.enter_context(patch(
        "shared.kraken_pair_floors.apply_floor",
        fake_apply_floor, create=True,
    ))
    stack.enter_context(patch(
        "shared.market_hours.is_equity_rth", return_value=market_open,
    ))
    stack.enter_context(patch(
        "shared.market_hours.is_equity_extended_hours",
        return_value=market_open,
    ))
    stack.enter_context(patch(
        "shared.market_hours.market_hours_reason",
        return_value="test-market-open" if market_open else "closed",
    ))
    stack.enter_context(patch(
        "routes.equity_extended_hours_admin.get_equity_extended_hours_enabled",
        new=AsyncMock(return_value=ext_hours_on), create=True,
    ))

    return stack, updated_docs, executions_mod, risk_mod


# ═══════════════════════════════════════════════════════════════════
# 3. Arbiter size_multiplier applied on crypto lane
# ═══════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_arbiter_size_multiplier_reduces_notional_before_risk_check():
    """Crypto intent notional $10, seat mult 1.0, evidence.size_multiplier
    0.5 → risk.check sees $5.0. sizing_degradation stamp records
    arbiter_multiplier=0.5."""
    from shared.auto_router_stages import _gate_risk

    intent = {
        "intent_id": "test-arb-mult-1", "symbol": "BTC/USD",
        "action": "BUY", "lane": "crypto",
        "stack": "camino", "ingest_ts": "2026-07-20T00:00:00+00:00",
        "requested_notional_usd": 10.0,
        "evidence": {"size_multiplier": 0.5},
    }
    sd = _seat_fire(lane="crypto", risk_multiplier=1.0)
    ctx = _make_ctx(intent, sd, notional_raw=10.0)

    seen_notionals: list[float] = []

    async def capture(_intent, *, notional_usd):
        seen_notionals.append(notional_usd)
        return _RiskOK(notional_usd=notional_usd)

    stack, updated_docs, execs_mod, risk_mod = _install_mocks(
        risk_check=capture, per_order_cap=1000.0,
    )
    with stack:
        verdict = await _gate_risk(ctx)

    # Should NOT short-circuit — arb×seat×base is 5.0 (>0)
    assert verdict is None, f"unexpected short-circuit: {verdict}"
    assert seen_notionals == [5.0], (
        f"risk.check saw {seen_notionals}, expected [5.0] "
        "(10.0 base * 1.0 seat * 0.5 arb)"
    )

    # sizing_degradation stamp with arbiter_multiplier=0.5
    stamps = [d["update"]["$set"] for d in updated_docs
              if "sizing_degradation" in d["update"].get("$set", {})]
    assert len(stamps) == 1, f"expected one sizing_degradation stamp; got {stamps}"
    sd_doc = stamps[0]["sizing_degradation"]
    assert sd_doc["arbiter_multiplier"] == 0.5
    assert sd_doc["seat_multiplier"] == 1.0
    assert sd_doc["base_usd"] == 10.0
    assert sd_doc["final_usd"] == 5.0
    assert sd_doc["floor_sized_up"] is False


# ═══════════════════════════════════════════════════════════════════
# 4. Equity intent under floor sized UP to $5
# ═══════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_equity_sub_floor_sized_up_to_five(monkeypatch):
    """Equity intent whose post-multiplier notional is $1.75 gets
    sized UP to $5.00. sizing_degradation.floor_sized_up=True and
    final_usd=5.0."""
    monkeypatch.setenv("WEBULL_MIN_NOTIONAL_USD", "5.00")
    # Clear webull_caps cache so no stale Mongo override interferes.
    import shared.broker.webull_caps as wc
    wc._CACHED_FLOOR_OVERRIDE = None
    wc._CACHED_FLOOR_TS = 0.0

    from shared.auto_router_stages import _gate_risk

    intent = {
        "intent_id": "test-equity-floor-1", "symbol": "AAPL",
        "action": "BUY", "lane": "equity",
        "stack": "camino", "ingest_ts": "2026-07-20T00:00:00+00:00",
        "requested_notional_usd": 3.5,
        "evidence": {"size_multiplier": 0.5},  # → $3.5 * 0.5 = $1.75
    }
    sd = _seat_fire(lane="equity", risk_multiplier=1.0)
    ctx = _make_ctx(intent, sd, notional_raw=3.5)

    seen: list[float] = []

    async def capture(_intent, *, notional_usd):
        seen.append(notional_usd)
        return _RiskOK(notional_usd=notional_usd)

    # per_order_cap high enough to allow the $5 sized-up amount
    stack, updated_docs, _execs, _risk = _install_mocks(
        risk_check=capture, per_order_cap=10.0,
    )
    with stack:
        verdict = await _gate_risk(ctx)

    assert verdict is None, f"unexpected short-circuit: {verdict}"
    # Risk should see the SIZED-UP $5.0, not the raw $1.75
    assert seen == [5.0], f"expected risk to see 5.0, got {seen}"

    stamps = [d["update"]["$set"] for d in updated_docs
              if "sizing_degradation" in d["update"].get("$set", {})]
    assert len(stamps) == 1
    sd_doc = stamps[0]["sizing_degradation"]
    assert sd_doc["floor_sized_up"] is True
    assert sd_doc["equity_floor_usd"] == 5.0
    assert sd_doc["final_usd"] == 5.0
    assert sd_doc["arbiter_multiplier"] == 0.5


# ═══════════════════════════════════════════════════════════════════
# 5. size_multiplier=0 → advisory_only + SIZED_TO_ZERO short-circuit
# ═══════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_size_multiplier_zero_short_circuits_advisory_only():
    """evidence.size_multiplier=0.0 → verdict='advisory_only',
    reason='SIZED_TO_ZERO', gate_state='advisory_only' (NOT blocked),
    broker_reason='SIZED_TO_ZERO', broker_error_bucket='conviction_sizing'.
    Executions row: broker_status='sized_to_zero'. NO risk.check call."""
    from shared.auto_router_stages import _gate_risk

    intent = {
        "intent_id": "test-sized-zero-1", "symbol": "AAPL",
        "action": "BUY", "lane": "equity",
        "stack": "camino", "ingest_ts": "2026-07-20T00:00:00+00:00",
        "requested_notional_usd": 10.0,
        "evidence": {"size_multiplier": 0.0},
    }
    sd = _seat_fire(lane="equity", risk_multiplier=1.0)
    ctx = _make_ctx(intent, sd, notional_raw=10.0)

    risk_check_called = {"n": 0}

    async def spy(_intent, *, notional_usd):
        risk_check_called["n"] += 1
        return _RiskOK(notional_usd=notional_usd)

    stack, updated_docs, execs_mod, _risk = _install_mocks(risk_check=spy)
    with stack:
        verdict = await _gate_risk(ctx)

    assert verdict is not None, "expected short-circuit; got None"
    assert verdict["verdict"] == "advisory_only", (
        f"expected verdict=advisory_only, got {verdict}"
    )
    assert verdict["reason"] == "SIZED_TO_ZERO"
    assert verdict["arbiter_multiplier"] == 0.0

    # risk.check must NOT have run
    assert risk_check_called["n"] == 0

    # Intent doc stamped correctly
    stamps = [d["update"]["$set"] for d in updated_docs
              if "gate_state" in d["update"].get("$set", {})]
    assert len(stamps) == 1
    stamp = stamps[0]
    assert stamp["gate_state"] == "advisory_only", (
        f"gate_state must be 'advisory_only', got {stamp['gate_state']}"
    )
    assert stamp["broker_reason"] == "SIZED_TO_ZERO"
    assert stamp["broker_error_bucket"] == "conviction_sizing"
    # NOT blocked / NOT RISK_REJECTED
    assert stamp["gate_state"] != "blocked"
    assert stamp["broker_reason"] != "RISK_REJECTED"

    # Executions row written with sized_to_zero status
    execs_mod.record.assert_awaited_once()
    kwargs = execs_mod.record.await_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["broker_status"] == "sized_to_zero"
    assert kwargs["risk_reason"] == "sized_to_zero"
    assert kwargs["notional_usd"] == 0.0


# ═══════════════════════════════════════════════════════════════════
# 6. Equity floor > per-order cap → blocked honestly
# ═══════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_equity_floor_exceeds_per_order_cap_blocks(monkeypatch):
    """With per_order_cap=$4 (below the $5 floor), an equity intent
    that would need size-up blocks with
    `broker_reason='equity_floor_exceeds_per_order_cap'`."""
    monkeypatch.setenv("WEBULL_MIN_NOTIONAL_USD", "5.00")
    import shared.broker.webull_caps as wc
    wc._CACHED_FLOOR_OVERRIDE = None
    wc._CACHED_FLOOR_TS = 0.0

    from shared.auto_router_stages import _gate_risk

    intent = {
        "intent_id": "test-eq-cap-1", "symbol": "AAPL",
        "action": "BUY", "lane": "equity",
        "stack": "camino", "ingest_ts": "2026-07-20T00:00:00+00:00",
        "requested_notional_usd": 3.0,
        "evidence": {"size_multiplier": 1.0},  # $3 stays $3, needs size-up
    }
    sd = _seat_fire(lane="equity", risk_multiplier=1.0)
    ctx = _make_ctx(intent, sd, notional_raw=3.0)

    stack, updated_docs, execs_mod, _risk = _install_mocks(per_order_cap=4.0)
    with stack:
        verdict = await _gate_risk(ctx)

    assert verdict is not None
    assert verdict["verdict"] == "blocked"
    assert verdict["reason"] == "equity_floor_exceeds_per_order_cap"
    assert verdict["floor_usd"] == 5.0
    assert verdict["cap_usd"] == 4.0

    stamps = [d["update"]["$set"] for d in updated_docs
              if "gate_state" in d["update"].get("$set", {})]
    assert len(stamps) == 1
    stamp = stamps[0]
    assert stamp["gate_state"] == "blocked"
    assert stamp["broker_reason"] == "equity_floor_exceeds_per_order_cap"
    assert stamp["broker_error_bucket"] == "min_order_notional"

    execs_mod.record.assert_awaited_once()
    kwargs = execs_mod.record.await_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["broker_status"] == "blocked_by_cap_authority"


# ═══════════════════════════════════════════════════════════════════
# 7. LIVE API smoke — auto-router still healthy under new stage
# ═══════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def live_token():
    if not BASE_URL:
        pytest.skip("REACT_APP_BACKEND_URL not set")
    r = requests.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": "admin@risedual.io",
              "password": "risedual-admin-2026"},
        timeout=15,
    )
    if r.status_code != 200:
        pytest.skip(f"login failed: {r.status_code} {r.text[:200]}")
    d = r.json()
    tok = d.get("access_token") or d.get("token")
    if not tok:
        pytest.skip(f"no token: {d}")
    return tok


def test_live_auto_router_status_task_alive(live_token):
    r = requests.get(
        f"{BASE_URL}/api/admin/auto-router/status",
        headers={"Authorization": f"Bearer {live_token}"},
        timeout=20,
    )
    assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
    d = r.json()
    assert d["task_alive"] is True, f"task_alive not True: {d}"
    assert d["last_tick_error"] in (None, ""), (
        f"last_tick_error non-null: {d.get('last_tick_error')}"
    )


def test_live_auto_router_force_tick_ok(live_token):
    r = requests.post(
        f"{BASE_URL}/api/admin/auto-router/force-tick",
        headers={"Authorization": f"Bearer {live_token}"},
        timeout=90,
    )
    assert r.status_code == 200, f"{r.status_code}: {r.text[:200]}"
    d = r.json()
    assert d.get("ok") is True, f"force-tick returned not-ok: {d}"
    # Preview arbiter is DISARMED — 0 results expected
    assert "results_count" in d
