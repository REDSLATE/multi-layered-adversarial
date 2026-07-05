"""Live Execution Path — end-to-end regression suite (2026-02-27 doctrine).

The doctrine, pinned by the operator on 2026-02-27:

    Market Data → Brain (emit intent) → Seat → Risk → Broker → Executions

Every unexecuted, routable intent in `shared_intents` walks through
`shared/auto_router.py::_route_one`. That function is the ONLY orchestrator
between an intent and the broker in the current codebase — there is no
20-gate legacy chain, no unified-pipeline receipt writer, no council
vote. One pass, one execution row, one broker call.

These tests lock in the CURRENT pipeline's behavior against silent
regression. They replace the 18 orphaned tests that were deleted during
the 2026-02-25 pipeline reduction and had been asserting against the
long-defunct 20-gate model.

What each stage MUST do:

    1. Seat.decide(intent)
       * verdict='pass' → intent gets stamped `advisory_only` (non-
         directional actions like HOLD) or `blocked` (any other pass
         reason). Broker is never called.
       * verdict='fire' → risk_multiplier is applied to the intent's
         notional before Risk sees it.

    2. Risk.check(intent, notional_usd=seat-adjusted)
       * ok=False → intent stamped `blocked`. Broker is never called.
       * ok=True → proceed with rc.notional_usd.

    2b. Kraken pair-floor (crypto lane only)
       * size_up policy raises rc.notional_usd to the pair floor.
       * reject policy terminates the intent with
         `broker_reason=notional_below_pair_floor`,
         `broker_error_bucket=min_order_notional`.

    3. broker_router.route_order(intent, notional_usd=final)
       * BrokerRouteBlocked / any exception → NOT a broker success.
         Terminal errors stamp `blocked` on first attempt; transient
         errors bump `broker_retry_count`.
       * success → intent stamped `executed=True` `gate_state=submitted`;
         broker_order embedded on the intent doc.

    4. executions.record(...)
       * EVERY attempt writes exactly one row — regardless of outcome.
       * All 4 seat holders + all 4 angel names are stamped on the row
         so the Auditor role has a joins-free audit trail.

If any test in this file fails, the live execution path has drifted
from the doctrine — do not merge the change until the drift is
either intentional (with the test updated) or reverted.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")


# ─── Helpers ────────────────────────────────────────────────────────

def _intent(**overrides):
    base = {
        "intent_id": "test-intent-live-path",
        "symbol": "AAPL",
        "action": "BUY",
        "lane": "equity",
        "stack": "camino",
        "ingest_ts": "2026-02-27T00:00:00+00:00",
        "requested_notional_usd": 10.0,
    }
    base.update(overrides)
    return base


def _seat_fire(
    brain="camino",
    lane="equity",
    strategist="camino",
    executor="camino",
    governor="hellcat",
    auditor="barracuda",
    risk_multiplier=1.0,
    reason="strategist_proposes",
):
    """Stand-in that matches SeatDecision's shape without importing it
    (so a MagicMock'd shared.seat module doesn't hand back a Mock
    object for the constructor)."""
    return SimpleNamespace(
        verdict="fire",
        reason=reason,
        lane=lane,
        intent_brain=brain,
        strategist=strategist,
        governor=governor,
        executor=executor,
        auditor=auditor,
        angels={
            "strategist": "Raziel", "governor": "Nuriel",
            "executor": "Paschar", "auditor": "Sariel",
        },
        risk_multiplier=risk_multiplier,
    )


def _seat_pass(
    lane="equity",
    reason="non_routable_action:'HOLD'",
    executor="camino",
):
    return SimpleNamespace(
        verdict="pass",
        reason=reason,
        lane=lane,
        intent_brain="camino",
        strategist="camino",
        governor="hellcat",
        executor=executor,
        auditor="barracuda",
        angels={
            "strategist": "Raziel", "governor": "Nuriel",
            "executor": "Paschar", "auditor": "Sariel",
        },
        risk_multiplier=1.0,
    )


class _RiskOK:
    def __init__(self, notional_usd=10.0):
        self.ok = True
        self.notional_usd = notional_usd
        self.reason = "ok"
        self.cap_per_order_usd = 10.0
        self.cap_daily_usd = 1000.0
        self.spent_today_usd = 0.0


class _RiskBlock:
    def __init__(self, reason="lane_disabled:equity"):
        self.ok = False
        self.notional_usd = 0.0
        self.reason = reason
        self.cap_per_order_usd = 10.0
        self.cap_daily_usd = 1000.0
        self.spent_today_usd = 0.0


@pytest.fixture
def route_one_scaffold():
    """Motor-shaped update-capturing fake plus module mocks. Returns a
    dict with knobs the caller can flip per-test."""
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

    seat_mod = MagicMock()
    risk_mod = MagicMock()

    return {
        "ar": ar,
        "fake_db": fake_db,
        "updated_docs": updated_docs,
        "executions_mod": executions_mod,
        "seat_mod": seat_mod,
        "risk_mod": risk_mod,
    }


def _apply_patches(s, *, broker_result=None, broker_raises=None, floor_result=None):
    """Assemble the standard patch stack for `_route_one`. Returns the
    context manager that the test must enter with `with ... :`."""
    from contextlib import ExitStack

    ar = s["ar"]
    broker_calls: list[dict] = []

    async def fake_route_order(intent, notional_usd, client_order_id):  # noqa: ARG001
        broker_calls.append({
            "intent_id": intent.get("intent_id"),
            "notional_usd": notional_usd,
            "client_order_id": client_order_id,
        })
        if broker_raises is not None:
            raise broker_raises
        return broker_result or {
            "id": "order-42",
            "order_id": "order-42",
            "broker": "webull",
            "status": "submitted",
            "lane": intent.get("lane"),
            "side": intent.get("action"),
            "notional": notional_usd,
        }

    floor_calls: list[dict] = []

    async def fake_apply_floor(pair, notional):
        floor_calls.append({"pair": pair, "notional": notional})
        if floor_result is not None:
            return floor_result
        # Default: pair floor is inert — pass-through.
        return SimpleNamespace(
            allowed=True,
            notional_usd=notional,
            reject_reason=None,
            adjusted=False,
            original_notional=notional,
            floor=SimpleNamespace(pair=pair, min_notional_usd=0.0, policy="size_up", is_default=True),
        )

    stack = ExitStack()
    stack.enter_context(patch.dict(sys.modules, {
        "shared.seat": s["seat_mod"],
        "shared.risk": s["risk_mod"],
        "shared.executions": s["executions_mod"],
    }))
    stack.enter_context(patch.object(ar, "db", s["fake_db"]))
    stack.enter_context(patch(
        "shared.broker_router.route_order", fake_route_order, create=True,
    ))
    stack.enter_context(patch(
        "shared.kraken_pair_floors.apply_floor", fake_apply_floor, create=True,
    ))

    s["broker_calls"] = broker_calls
    s["floor_calls"] = floor_calls
    return stack


# ═══════════════════════════════════════════════════════════════════
# 1. HAPPY PATH
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_happy_path_intent_gets_executed(route_one_scaffold):
    """Seat fires → Risk clears → Broker accepts → intent stamped
    `executed=True gate_state=submitted`; execution row written with
    `ok=True`. This is the doctrine's blessed sunny-day flow."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire())
    s["risk_mod"].check = AsyncMock(return_value=_RiskOK())

    with _apply_patches(s):
        r = await s["ar"]._route_one(_intent())

    assert r["verdict"] == "executed"
    assert r["broker"] == "webull"
    assert r["order_id"] == "order-42"

    # Intent must be terminally stamped as submitted.
    stamps = [
        d["update"]["$set"] for d in s["updated_docs"]
        if "gate_state" in d["update"].get("$set", {})
    ]
    assert len(stamps) == 1, f"expected one gate_state stamp; got {stamps}"
    assert stamps[0]["gate_state"] == "submitted"
    assert stamps[0]["executed"] is True

    # Broker was called exactly once.
    assert len(s["broker_calls"]) == 1

    # Executions row was written with ok=True.
    s["executions_mod"].record.assert_awaited_once()
    kwargs = s["executions_mod"].record.await_args.kwargs
    assert kwargs["ok"] is True
    assert kwargs["broker"] == "webull"


# ═══════════════════════════════════════════════════════════════════
# 2. SEAT-LEVEL BLOCKS
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_seat_pass_stamps_advisory_only_and_skips_broker(route_one_scaffold):
    """When Seat returns verdict='pass', the intent MUST be stamped
    `advisory_only` (the operator-visible marker for "brain emitted
    but seat rejected") and the broker MUST NOT be called."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_pass(
        reason="non_routable_action:'HOLD'",
    ))
    s["risk_mod"].check = AsyncMock(return_value=_RiskOK())

    with _apply_patches(s):
        r = await s["ar"]._route_one(_intent(action="HOLD"))

    assert r["verdict"] == "blocked"
    assert r["reason"] == "non_routable_action:'HOLD'"

    # No broker call.
    assert len(s["broker_calls"]) == 0

    # Intent stamped advisory_only (because seat verdict was 'pass',
    # not 'block' — that distinction matters downstream for the funnel).
    stamps = [d["update"]["$set"] for d in s["updated_docs"]
              if "gate_state" in d["update"].get("$set", {})]
    assert len(stamps) == 1
    assert stamps[0]["gate_state"] == "advisory_only"
    assert stamps[0]["seat_reason"] == "non_routable_action:'HOLD'"

    # Executions row was still written (audit is unconditional) — with ok=False.
    s["executions_mod"].record.assert_awaited_once()
    kwargs = s["executions_mod"].record.await_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["risk_reason"] == "seat_did_not_fire"


@pytest.mark.asyncio
async def test_vacant_executor_seat_blocks_intent_before_broker(route_one_scaffold):
    """Doctrine (seat.py:360): if the executor seat is VACANT, the
    lane has no authority to route. Seat returns `verdict=pass` with
    reason `executor_seat_vacant:<lane>` — this is the marker the
    operator watches when a lane needs a seat assigned."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_pass(
        reason="executor_seat_vacant:equity",
        executor=None,
    ))
    s["risk_mod"].check = AsyncMock(return_value=_RiskOK())

    with _apply_patches(s):
        r = await s["ar"]._route_one(_intent())

    assert r["verdict"] == "blocked"
    assert "executor_seat_vacant" in r["reason"]
    assert len(s["broker_calls"]) == 0


# ═══════════════════════════════════════════════════════════════════
# 3. GOVERNOR RISK-MULTIPLIER APPLIED BEFORE RISK / BROKER
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_governor_multiplier_reduces_notional_before_risk_check(route_one_scaffold):
    """The governor's `risk_multiplier` is applied to the intent's
    requested notional BEFORE risk.check sees it. A 0.5 governor on
    a $10 requested intent means Risk evaluates $5. This is how the
    governor's "size regime" doctrine actually bites the notional."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire(risk_multiplier=0.5))

    seen: list[float] = []

    async def capture_risk(_intent, *, notional_usd):
        seen.append(notional_usd)
        return _RiskOK(notional_usd=notional_usd)

    s["risk_mod"].check = capture_risk

    with _apply_patches(s):
        await s["ar"]._route_one(_intent(requested_notional_usd=10.0))

    # 10.0 * 0.5 governor mult = 5.0 seen by Risk (and thus broker).
    assert seen == [5.0]
    assert s["broker_calls"][0]["notional_usd"] == 5.0


@pytest.mark.asyncio
async def test_council_participant_gets_50pct_dampener(route_one_scaffold):
    """Doctrine (seat.py:388): a non-seat brain's intent routes as a
    council participant at 50% of the governor's multiplier. Verified
    here by driving the seat with a 0.50 effective mult (governor 1.0
    × council 0.5)."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire(
        brain="hellcat",  # not the strategist or executor
        risk_multiplier=0.5,  # governor 1.0 × council 0.5
        reason="non_seat_brain_routes_to_executor:hellcat (routing as council participant at 50% size)",
    ))
    seen: list[float] = []

    async def capture_risk(_intent, *, notional_usd):
        seen.append(notional_usd)
        return _RiskOK(notional_usd=notional_usd)

    s["risk_mod"].check = capture_risk

    with _apply_patches(s):
        await s["ar"]._route_one(_intent(requested_notional_usd=10.0))

    assert seen == [5.0]  # 10.0 * 0.5 council-effective mult
    assert s["broker_calls"][0]["notional_usd"] == 5.0


# ═══════════════════════════════════════════════════════════════════
# 4. RISK-LEVEL BLOCKS
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_risk_block_stamps_intent_and_skips_broker(route_one_scaffold):
    """When Risk says no (e.g. lane disabled, daily cap exceeded,
    master freeze on), the intent MUST be stamped `blocked` and the
    broker MUST NOT be called."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire())
    s["risk_mod"].check = AsyncMock(return_value=_RiskBlock(reason="lane_disabled:equity"))

    with _apply_patches(s):
        r = await s["ar"]._route_one(_intent())

    assert r["verdict"] == "blocked"
    assert r["reason"] == "lane_disabled:equity"
    assert len(s["broker_calls"]) == 0

    stamps = [d["update"]["$set"] for d in s["updated_docs"]
              if "gate_state" in d["update"].get("$set", {})]
    assert len(stamps) == 1
    assert stamps[0]["gate_state"] == "blocked"
    assert stamps[0]["risk_reason"] == "lane_disabled:equity"

    # Audit row written with risk_ok=False.
    kwargs = s["executions_mod"].record.await_args.kwargs
    assert kwargs["risk_ok"] is False
    assert kwargs["ok"] is False


# ═══════════════════════════════════════════════════════════════════
# 5. KRAKEN PAIR-FLOOR (CRYPTO LANE ONLY)
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_crypto_pair_floor_size_up_raises_broker_notional(route_one_scaffold):
    """Doctrine: crypto intents whose notional falls below a pair's
    configured `min_notional_usd` are SIZED UP to the floor before
    being sent to Kraken. Equity lane is untouched."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire(lane="crypto"))
    s["risk_mod"].check = AsyncMock(return_value=_RiskOK(notional_usd=3.0))

    floor_result = SimpleNamespace(
        allowed=True,
        notional_usd=10.0,          # sized up to the floor
        reject_reason=None,
        adjusted=True,
        original_notional=3.0,
        floor=SimpleNamespace(
            pair="BTC/USD", min_notional_usd=10.0, policy="size_up", is_default=False,
        ),
    )

    with _apply_patches(s, floor_result=floor_result):
        r = await s["ar"]._route_one(_intent(
            lane="crypto", symbol="BTC/USD", requested_notional_usd=3.0,
        ))

    assert r["verdict"] == "executed"
    # Broker saw the FLOORED notional, not the original $3.
    assert s["broker_calls"][0]["notional_usd"] == 10.0
    # apply_floor was consulted with the requested pair.
    assert s["floor_calls"] == [{"pair": "BTC/USD", "notional": 3.0}]


@pytest.mark.asyncio
async def test_crypto_pair_floor_reject_policy_terminates_intent(route_one_scaffold):
    """When the operator has set `policy=reject` for a pair and the
    intent falls under the floor, the intent MUST be terminally
    stamped `blocked` with reason `notional_below_pair_floor` and
    bucket `min_order_notional`. Broker MUST NOT be called."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire(lane="crypto"))
    s["risk_mod"].check = AsyncMock(return_value=_RiskOK(notional_usd=3.0))

    floor_result = SimpleNamespace(
        allowed=False,
        notional_usd=3.0,
        reject_reason="notional_below_pair_floor: $3.0000 < $10.0000 for BTC/USD",
        adjusted=False,
        original_notional=3.0,
        floor=SimpleNamespace(
            pair="BTC/USD", min_notional_usd=10.0, policy="reject", is_default=False,
        ),
    )

    with _apply_patches(s, floor_result=floor_result):
        r = await s["ar"]._route_one(_intent(
            lane="crypto", symbol="BTC/USD", requested_notional_usd=3.0,
        ))

    assert r["verdict"] == "blocked"
    assert r["reason"] == "notional_below_pair_floor"
    assert len(s["broker_calls"]) == 0

    stamps = [d["update"]["$set"] for d in s["updated_docs"]
              if "gate_state" in d["update"].get("$set", {})]
    assert len(stamps) == 1
    assert stamps[0]["gate_state"] == "blocked"
    assert stamps[0]["broker_reason"] == "notional_below_pair_floor"
    assert stamps[0]["broker_error_bucket"] == "min_order_notional"


@pytest.mark.asyncio
async def test_equity_intent_never_consults_pair_floor(route_one_scaffold):
    """Kraken's pair-floor doctrine is CRYPTO-ONLY. An equity intent
    must never call apply_floor — otherwise Webull orders would be
    mis-sized by Kraken's per-pair minimums."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire(lane="equity"))
    s["risk_mod"].check = AsyncMock(return_value=_RiskOK())

    with _apply_patches(s):
        await s["ar"]._route_one(_intent(lane="equity", symbol="AAPL"))

    assert s["floor_calls"] == [], (
        "Equity intent triggered apply_floor — this is a cross-lane "
        "doctrine violation. Only crypto intents should touch Kraken's "
        "per-pair floor system."
    )


# ═══════════════════════════════════════════════════════════════════
# 6. BROKER-LEVEL BLOCKS
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_broker_route_blocked_stamps_intent_and_records_execution(route_one_scaffold):
    """A `BrokerRouteBlocked` exception from broker_router indicates a
    NO_TRADE condition — must terminate the intent immediately with
    `broker_status=blocked_by_broker_router` on the execution row."""
    from shared.broker_router import BrokerRouteBlocked

    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire())
    s["risk_mod"].check = AsyncMock(return_value=_RiskOK())

    with _apply_patches(s, broker_raises=BrokerRouteBlocked("no_route_for_symbol")):
        r = await s["ar"]._route_one(_intent())

    assert r["verdict"] == "blocked"
    assert "no_route_for_symbol" in r["reason"]

    stamps = [d["update"]["$set"] for d in s["updated_docs"]
              if "gate_state" in d["update"].get("$set", {})]
    assert len(stamps) == 1
    assert stamps[0]["gate_state"] == "blocked"

    kwargs = s["executions_mod"].record.await_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["broker_status"] == "blocked_by_broker_router"
    assert kwargs["exception_type"] == "BrokerRouteBlocked"


# ═══════════════════════════════════════════════════════════════════
# 7. AUDIT-TRAIL COMPLETENESS (Auditor role's raison d'être)
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_execution_row_stamps_all_four_seat_holders_and_angels(route_one_scaffold):
    """The Auditor's post-pass review can't join across collections —
    it needs the full seat context on ONE row. All 4 role holders and
    all 4 angel names must be stamped on the execution row for every
    attempt (success or failure)."""
    s = route_one_scaffold
    seat = _seat_fire(
        strategist="gto",
        governor="hellcat",
        executor="camino",
        auditor="barracuda",
    )
    s["seat_mod"].decide = AsyncMock(return_value=seat)
    s["risk_mod"].check = AsyncMock(return_value=_RiskOK())

    with _apply_patches(s):
        await s["ar"]._route_one(_intent())

    kwargs = s["executions_mod"].record.await_args.kwargs
    assert kwargs["strategist"] == "gto"
    assert kwargs["governor"] == "hellcat"
    assert kwargs["executor"] == "camino"
    assert kwargs["auditor"] == "barracuda"
    assert kwargs["angels"] == {
        "strategist": "Raziel", "governor": "Nuriel",
        "executor": "Paschar", "auditor": "Sariel",
    }
    assert kwargs["risk_multiplier"] == 1.0


@pytest.mark.asyncio
async def test_exactly_one_execution_row_per_attempt(route_one_scaffold):
    """One row per attempt — regardless of outcome. This is what makes
    `executions.count()` a meaningful denominator for the funnel."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire())
    s["risk_mod"].check = AsyncMock(return_value=_RiskOK())

    with _apply_patches(s):
        await s["ar"]._route_one(_intent())

    assert s["executions_mod"].record.await_count == 1


# ═══════════════════════════════════════════════════════════════════
# 8. IDEMPOTENCY (executed=True stamp)
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_success_stamps_executed_true_with_broker_order_embedded(route_one_scaffold):
    """A successful broker call MUST set `executed=True` on the intent
    and embed the broker_order fields — this is what makes the next
    auto-router tick skip the intent (via the query filter
    `executed: {"$ne": True}`)."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire())
    s["risk_mod"].check = AsyncMock(return_value=_RiskOK())

    with _apply_patches(s, broker_result={
        "id": "wb-1234",
        "order_id": "wb-1234",
        "broker": "webull",
        "status": "submitted",
        "lane": "equity",
        "side": "BUY",
        "qty": 0.05,
        "notional": 10.0,
        "filled_qty": 0.0,
        "submitted_at": "2026-02-27T10:00:00Z",
    }):
        await s["ar"]._route_one(_intent())

    stamps = [d["update"]["$set"] for d in s["updated_docs"]
              if d["update"].get("$set", {}).get("executed") is True]
    assert len(stamps) == 1
    stamp = stamps[0]
    assert stamp["executed"] is True
    assert stamp["gate_state"] == "submitted"
    order = stamp["broker_order"]
    assert order["id"] == "wb-1234"
    assert order["broker"] == "webull"
    assert order["status"] == "submitted"
    assert order["notional"] == 10.0
