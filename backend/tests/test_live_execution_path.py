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
    # The 2026-02-28 cap-authority guard reads `risk.per_order_cap()`
    # to compare against a pair-floor size-up. Default the mock high
    # enough that it never conflicts unless the test explicitly lowers
    # it — otherwise every pair-floor test would trip the guard.
    risk_mod.per_order_cap = MagicMock(return_value=1000.0)

    return {
        "ar": ar,
        "fake_db": fake_db,
        "updated_docs": updated_docs,
        "executions_mod": executions_mod,
        "seat_mod": seat_mod,
        "risk_mod": risk_mod,
    }


def _apply_patches(s, *, broker_result=None, broker_raises=None, floor_result=None,
                   market_open=True, extended_hours_enabled=False):
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
    # 2026-07-06 — equity market-closed pre-flight gate added.
    # These tests exercise the sunny-day pipeline and assume the
    # market is open. Patch the RTH gate and the extended-hours flag
    # so scaffold-based tests behave the same on any wall clock.
    # `market_open=False` flips this to exercise the pre-flight block.
    stack.enter_context(patch(
        "shared.market_hours.is_equity_rth", return_value=market_open,
    ))
    stack.enter_context(patch(
        "shared.market_hours.is_equity_extended_hours",
        return_value=market_open,
    ))
    stack.enter_context(patch(
        "shared.market_hours.market_hours_reason",
        return_value=(
            "test-scaffold-market-open" if market_open
            else "equity_after_hours: test scaffold; next open ..."
        ),
    ))
    stack.enter_context(patch(
        "routes.equity_extended_hours_admin.get_equity_extended_hours_enabled",
        new=AsyncMock(return_value=extended_hours_enabled), create=True,
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
    being sent to Kraken. Equity lane is untouched.

    Ordering (2026-02-28 doctrine): pair-floor runs BEFORE risk, so
    risk sees the FLOORED notional (never the pre-floor value)."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire(lane="crypto"))
    # Echo risk: return whatever notional was passed in.
    risk_seen: list[float] = []

    async def echo_risk(_intent, *, notional_usd):
        risk_seen.append(notional_usd)
        return _RiskOK(notional_usd=notional_usd)

    s["risk_mod"].check = echo_risk

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
    # apply_floor was consulted with the requested pair BEFORE risk.
    assert s["floor_calls"] == [{"pair": "BTC/USD", "notional": 3.0}]
    # Risk received the FLOORED value (10.0), not the raw pre-floor
    # value (3.0). This is the key ordering assertion of the 2026-02-28
    # reorder — risk is authoritative on the actual shipped notional.
    assert risk_seen == [10.0]
    # Audit row and return payload MUST reflect the shipped amount.
    kwargs = s["executions_mod"].record.await_args.kwargs
    assert kwargs["notional_usd"] == 10.0
    assert r["final_notional"] == 10.0
    assert r["notional_usd"] == 10.0


@pytest.mark.asyncio
async def test_crypto_pair_floor_reject_policy_terminates_intent(route_one_scaffold):
    """When the operator has set `policy=reject` for a pair and the
    intent falls under the floor, the intent MUST be terminally
    stamped `blocked` with reason `notional_below_pair_floor` and
    bucket `min_order_notional`. Broker MUST NOT be called.

    2026-02-28 doctrine fix: the reject path also MUST call
    `executions.record()` — pre-fix, this path skipped the audit,
    breaking the "one row per attempt" contract and the funnel
    denominator."""
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

    # 2026-02-28 audit-hole fix: the reject path MUST write an
    # executions row so the funnel counts this attempt.
    s["executions_mod"].record.assert_awaited_once()
    kwargs = s["executions_mod"].record.await_args.kwargs
    assert kwargs["ok"] is False
    assert kwargs["risk_reason"] == "pair_floor_reject"
    assert kwargs["broker_status"] == "blocked_by_pair_floor"
    assert kwargs["exception_type"] == "PairFloorReject"


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


# ═══════════════════════════════════════════════════════════════════
# 9. 2026-02-28 DOCTRINE PATCH — cap-authority + audit-truth + expiry
# ═══════════════════════════════════════════════════════════════════
# These tests lock in the 6-item drift-review fix delivered on
# 2026-02-28. Each corresponds to one identified drift point.


@pytest.mark.asyncio
async def test_pair_floor_exceeding_per_order_cap_blocks_intent(route_one_scaffold):
    """DRIFT #1 (safety hole): Kraken pair-floor sizes crypto orders up.
    If the floor exceeds the operator-set per-order cap, the pre-fix
    code sent the un-clipped floor to the broker, silently bypassing
    the cap. The fix blocks with `pair_floor_exceeds_per_order_cap`.
    Cap is authority; floor is exchange constraint."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire(lane="crypto"))
    s["risk_mod"].check = AsyncMock(return_value=_RiskOK())
    # Operator-set cap: $10. Kraken floor for this pair: $15.
    s["risk_mod"].per_order_cap = MagicMock(return_value=10.0)

    floor_result = SimpleNamespace(
        allowed=True,
        notional_usd=15.0,          # floor > cap
        reject_reason=None,
        adjusted=True,
        original_notional=5.0,
        floor=SimpleNamespace(
            pair="XRP/USD", min_notional_usd=15.0, policy="size_up", is_default=False,
        ),
    )

    with _apply_patches(s, floor_result=floor_result):
        r = await s["ar"]._route_one(_intent(
            lane="crypto", symbol="XRP/USD", requested_notional_usd=5.0,
        ))

    assert r["verdict"] == "blocked"
    assert r["reason"] == "pair_floor_exceeds_per_order_cap"
    assert r["floor_usd"] == 15.0
    assert r["cap_usd"] == 10.0
    assert r["pair"] == "XRP/USD"

    # Broker MUST NOT be called — the cap said no.
    assert len(s["broker_calls"]) == 0

    # Intent terminally stamped with the cap-authority reason.
    stamps = [d["update"]["$set"] for d in s["updated_docs"]
              if "gate_state" in d["update"].get("$set", {})]
    assert len(stamps) == 1
    assert stamps[0]["gate_state"] == "blocked"
    assert stamps[0]["broker_reason"] == "pair_floor_exceeds_per_order_cap"
    assert stamps[0]["broker_error_bucket"] == "min_order_notional"

    # Audit row written (one-row-per-attempt doctrine).
    s["executions_mod"].record.assert_awaited_once()
    kwargs = s["executions_mod"].record.await_args.kwargs
    assert kwargs["ok"] is False
    assert "pair_floor_exceeds_per_order_cap" in kwargs["risk_reason"]
    assert kwargs["broker_status"] == "blocked_by_cap_authority"


@pytest.mark.asyncio
async def test_pair_floor_at_or_below_cap_passes_through(route_one_scaffold):
    """Negative regression for the guard: a floor equal to the cap
    (edge case) or below it must NOT trip the block. Otherwise the
    guard would false-positive on every normal size-up."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire(lane="crypto"))
    s["risk_mod"].per_order_cap = MagicMock(return_value=10.0)

    risk_seen: list[float] = []

    async def echo_risk(_intent, *, notional_usd):
        risk_seen.append(notional_usd)
        return _RiskOK(notional_usd=notional_usd)

    s["risk_mod"].check = echo_risk

    floor_result = SimpleNamespace(
        allowed=True, notional_usd=10.0,        # floor == cap (edge)
        reject_reason=None, adjusted=True, original_notional=3.0,
        floor=SimpleNamespace(
            pair="BTC/USD", min_notional_usd=10.0,
            policy="size_up", is_default=False,
        ),
    )

    with _apply_patches(s, floor_result=floor_result):
        r = await s["ar"]._route_one(_intent(
            lane="crypto", symbol="BTC/USD", requested_notional_usd=3.0,
        ))

    assert r["verdict"] == "executed"
    assert risk_seen == [10.0]
    assert s["broker_calls"][0]["notional_usd"] == 10.0


@pytest.mark.asyncio
async def test_equity_risk_downsize_ships_clipped_notional_not_raw(route_one_scaffold):
    """DRIFT #6 (audit truth): risk silently clips to per-order cap.
    Broker/audit/return MUST use the CLIPPED value — not the raw
    governor-scaled value. Pre-fix, broker received the raw amount
    (past cap) while audit recorded the clipped value — the two lied
    about each other."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire(lane="equity"))
    # Risk downsizes 100 → 10 (per-order cap).
    async def clip_risk(_intent, *, notional_usd):  # noqa: ARG001
        return _RiskOK(notional_usd=10.0)
    s["risk_mod"].check = clip_risk

    with _apply_patches(s):
        r = await s["ar"]._route_one(_intent(
            lane="equity", symbol="AAPL", requested_notional_usd=100.0,
        ))

    assert r["verdict"] == "executed"
    # Broker MUST receive the clipped value, not the raw 100.
    assert s["broker_calls"][0]["notional_usd"] == 10.0
    # Audit MUST record the same value the broker got.
    kwargs = s["executions_mod"].record.await_args.kwargs
    assert kwargs["notional_usd"] == 10.0
    # Return payload MUST reflect the shipped amount.
    assert r["final_notional"] == 10.0
    assert r["notional_usd"] == 10.0


@pytest.mark.asyncio
async def test_success_stamps_final_notional_usd_on_intent(route_one_scaffold):
    """DRIFT #6 continued: the intent doc itself must carry the actual
    shipped notional in a durable field so the funnel and downstream
    joins don't have to reconstruct it from `broker_order`."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire(lane="crypto"))
    s["risk_mod"].per_order_cap = MagicMock(return_value=1000.0)

    async def echo_risk(_intent, *, notional_usd):
        return _RiskOK(notional_usd=notional_usd)
    s["risk_mod"].check = echo_risk

    floor_result = SimpleNamespace(
        allowed=True, notional_usd=25.0, reject_reason=None,
        adjusted=True, original_notional=5.0,
        floor=SimpleNamespace(
            pair="ETH/USD", min_notional_usd=25.0,
            policy="size_up", is_default=False,
        ),
    )
    with _apply_patches(s, floor_result=floor_result):
        await s["ar"]._route_one(_intent(
            lane="crypto", symbol="ETH/USD", requested_notional_usd=5.0,
        ))

    exec_stamp = next(
        d["update"]["$set"] for d in s["updated_docs"]
        if d["update"].get("$set", {}).get("executed") is True
    )
    assert exec_stamp["final_notional_usd"] == 25.0


# ─── Expired-unrouted sweeper (DRIFT #2) ─────────────────────────

@pytest.mark.asyncio
async def test_expired_unrouted_sweep_stamps_stale_intents():
    """DRIFT #2 (visibility gap): intents older than
    `AUTO_ROUTER_EXPIRE_MIN` that never reached a terminal state must
    be stamped `gate_state=expired_unrouted` so the operator can see
    aged-out intents in the funnel instead of them silently vanishing.

    2026-02-28 prod-hotfix refactor: the sweeper is now two-phase to
    protect against Mongo timeouts on multi-million-row prod:
      1. `find(...).limit(500).max_time_ms(3000)` collects intent_ids
      2. scoped `update_many({intent_id: $in [...]})` on the small set
    """
    from shared import auto_router as ar

    find_calls: list[dict] = []
    update_calls: list[dict] = []

    class FakeCursor:
        def __init__(self, docs):
            self.docs = docs
        def max_time_ms(self, ms):
            find_calls[-1]["max_time_ms"] = ms
            return self
        def limit(self, n):
            find_calls[-1]["limit"] = n
            return self
        def __aiter__(self):
            self._i = 0
            return self
        async def __anext__(self):
            if self._i >= len(self.docs):
                raise StopAsyncIteration
            d = self.docs[self._i]
            self._i += 1
            return d

    class FakeResult:
        def __init__(self, n): self.modified_count = n

    class FakeColl:
        def find(self, query, projection=None):
            find_calls.append({"query": query, "projection": projection})
            return FakeCursor([
                {"intent_id": f"stale-{i}"} for i in range(7)
            ])
        async def update_many(self, query, update):
            update_calls.append({"query": query, "update": update})
            return FakeResult(7)

    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(return_value=FakeColl())

    with patch.object(ar, "db", fake_db):
        stamped = await ar._sweep_expired_unrouted()

    assert stamped == 7
    # Phase 1: find scoped to old, non-executed, non-terminal intents
    # with a bounded batch and a Mongo-side deadline.
    assert len(find_calls) == 1
    q = find_calls[0]["query"]
    assert q["executed"] == {"$ne": True}
    assert q["ingest_ts"]["$lt"]  # some ISO cutoff
    excluded = set(q["gate_state"]["$nin"])
    assert {"blocked", "advisory_only", "submitted", "expired_unrouted"} <= excluded
    assert find_calls[0]["limit"] == 500
    assert find_calls[0]["max_time_ms"] == 3000

    # Phase 2: update_many restricted to the collected intent_ids
    # (never a broad range-scan).
    assert len(update_calls) == 1
    upd_q = update_calls[0]["query"]
    assert "intent_id" in upd_q
    assert upd_q["intent_id"]["$in"] == [f"stale-{i}" for i in range(7)]
    u = update_calls[0]["update"]["$set"]
    assert u["gate_state"] == "expired_unrouted"
    assert "expired_at" in u
    assert "aged_past_router_window" in u["expire_reason"]


@pytest.mark.asyncio
async def test_expired_unrouted_sweep_respects_expire_min_env(monkeypatch):
    """The expiry window MUST be env-tunable (`AUTO_ROUTER_EXPIRE_MIN`)
    so the operator can widen/narrow the visibility window without a
    redeploy. Default is 120 min per the 2026-02-28 doctrine."""
    from shared import auto_router as ar

    update_calls: list[dict] = []

    class FakeCursor:
        def max_time_ms(self, ms): return self
        def limit(self, n): return self
        def __aiter__(self):
            self._i = 0
            return self
        async def __anext__(self):
            if self._i >= 1:
                raise StopAsyncIteration
            self._i += 1
            return {"intent_id": "stale-0"}

    class FakeResult:
        def __init__(self, n): self.modified_count = n

    class FakeColl:
        def find(self, query, projection=None):
            return FakeCursor()
        async def update_many(self, query, update):
            update_calls.append({"query": query, "update": update})
            return FakeResult(0)

    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(return_value=FakeColl())

    monkeypatch.setenv("AUTO_ROUTER_EXPIRE_MIN", "30")
    with patch.object(ar, "db", fake_db):
        await ar._sweep_expired_unrouted()

    assert update_calls[0]["update"]["$set"]["expire_reason"] == (
        "aged_past_router_window:30min"
    )


@pytest.mark.asyncio
async def test_expired_unrouted_sweep_short_circuits_when_no_stale_intents():
    """DRIFT #2 refinement (2026-02-28): if phase-1 find returns
    empty, the sweeper must NOT call `update_many` at all. This
    saves an unnecessary DB round-trip on healthy ticks and — more
    importantly on prod — avoids a wide-range update on a hot
    collection when there's nothing to stamp."""
    from shared import auto_router as ar

    update_calls: list[dict] = []

    class EmptyCursor:
        def max_time_ms(self, ms): return self
        def limit(self, n): return self
        def __aiter__(self):
            return self
        async def __anext__(self):
            raise StopAsyncIteration

    class FakeColl:
        def find(self, query, projection=None):
            return EmptyCursor()
        async def update_many(self, query, update):
            update_calls.append({"query": query})
            return MagicMock(modified_count=99)

    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(return_value=FakeColl())

    with patch.object(ar, "db", fake_db):
        stamped = await ar._sweep_expired_unrouted()

    assert stamped == 0
    assert update_calls == [], (
        "sweeper called update_many on empty phase-1 result — "
        "wasted round-trip and potential wide scan on prod."
    )


@pytest.mark.asyncio
async def test_tick_query_excludes_expired_unrouted_intents():
    """The sample query MUST include `expired_unrouted` in its $nin so
    aged-out intents can never re-enter routing. Without this, the
    sweeper's stamp would be re-picked and immediately re-stamped in
    a hot loop."""
    from shared import auto_router as ar
    import inspect
    src = inspect.getsource(ar._tick)
    assert "expired_unrouted" in src, (
        "_tick's sample query must exclude `expired_unrouted` intents"
    )


@pytest.mark.asyncio
async def test_expire_min_default_is_120():
    """Default expiry window is DOUBLE the lookback (60 min → 120 min)
    so a legit late-arriving intent isn't cut off by racing the two
    windows. If a future refactor changes this constant, this test
    catches it deliberately."""
    import importlib
    from shared import auto_router as ar
    # Simulate no env var
    import os
    saved = os.environ.pop("AUTO_ROUTER_EXPIRE_MIN", None)
    try:
        importlib.reload(ar)
        assert ar.AUTO_ROUTER_EXPIRE_MIN == 120
    finally:
        if saved is not None:
            os.environ["AUTO_ROUTER_EXPIRE_MIN"] = saved
        importlib.reload(ar)


# ═══════════════════════════════════════════════════════════════════
# 11. EQUITY MARKET-CLOSED PRE-FLIGHT (2026-07-06)
# ═══════════════════════════════════════════════════════════════════
# Doctrine (operator, 2026-07-06):
#   Market closed is NOT a broker error, it is a known routing
#   condition. Equity intents outside RTH must be terminally blocked
#   BEFORE the Webull round-trip so we don't burn the API rate budget
#   on doomed orders. Crypto lane is untouched (Kraken 24/7).
#
# Before this gate: every Sunday equity intent hit Webull, got HTTP
# 417 "The time you sent is not supported", was classified
# `bucket=market_closed`, and terminal-stamped. Cost: ~2,000 wasted
# Webull calls/day + 19,223 error log lines + rising 429 rate-limit
# risk that threatened Monday's opening bell.


@pytest.mark.asyncio
async def test_market_closed_equity_intent_blocked_before_broker(route_one_scaffold):
    """Equity + market closed → NO broker.route_order call →
    executions row with `broker_status=market_closed_preflight`."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire())
    s["risk_mod"].check = AsyncMock(return_value=_RiskOK())

    with _apply_patches(s, market_open=False):
        r = await s["ar"]._route_one(_intent())

    # Broker was NEVER called.
    assert len(s["broker_calls"]) == 0

    # Verdict + reason.
    assert r["verdict"] == "blocked"
    assert r["reason"] == "market_closed_preflight"
    assert r["extended_hours_enabled"] is False
    assert "detail" in r

    # Audit row: one per attempt, market_closed_preflight status.
    assert s["executions_mod"].record.await_count == 1
    kwargs = s["executions_mod"].record.await_args.kwargs
    assert kwargs["broker_status"] == "market_closed_preflight"
    assert kwargs["ok"] is False

    # Intent stamped for the funnel: gate_state=blocked,
    # broker_error_bucket=market_closed (so the operator sees WHY),
    # broker_reason=market_closed_preflight (so it's distinguished
    # from a real broker-side rejection).
    stamps = [d["update"]["$set"] for d in s["updated_docs"]
              if "gate_state" in d["update"].get("$set", {})]
    assert len(stamps) == 1
    stamp = stamps[0]
    assert stamp["gate_state"] == "blocked"
    assert stamp["broker_reason"] == "market_closed_preflight"
    assert stamp["broker_error_bucket"] == "market_closed"
    assert "broker_error_detail" in stamp


@pytest.mark.asyncio
async def test_market_closed_crypto_intent_still_reaches_broker(route_one_scaffold):
    """Same market-closed clock, but crypto lane → gate does NOT
    fire → broker IS called (Kraken 24/7)."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire(lane="crypto"))
    s["risk_mod"].check = AsyncMock(return_value=_RiskOK())

    with _apply_patches(s, market_open=False):
        r = await s["ar"]._route_one(_intent(
            lane="crypto", symbol="BTC/USD",
        ))

    # Broker IS called for crypto regardless of clock.
    assert len(s["broker_calls"]) == 1
    assert r["verdict"] == "executed"


@pytest.mark.asyncio
async def test_market_open_equity_intent_reaches_broker(route_one_scaffold):
    """Market open sunny-day: equity gate passes → broker IS called.
    Baseline that the existing 22 scaffold-based tests already rely on;
    this pins it explicitly against the pre-flight gate."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire())
    s["risk_mod"].check = AsyncMock(return_value=_RiskOK())

    with _apply_patches(s, market_open=True):
        r = await s["ar"]._route_one(_intent())

    assert len(s["broker_calls"]) == 1
    assert r["verdict"] == "executed"


@pytest.mark.asyncio
async def test_after_hours_with_extended_flag_reaches_broker(route_one_scaffold):
    """Extended-hours flag ON + intent inside 04:00-20:00 ET window →
    broker IS called. The gate consults `is_equity_extended_hours`
    when the operator has flipped the runtime flag on."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire())
    s["risk_mod"].check = AsyncMock(return_value=_RiskOK())

    # market_open=True here means both is_equity_rth and
    # is_equity_extended_hours return True. The relevant flip is that
    # the extended-hours OPERATOR flag is now on — the gate should
    # honor it and pass through.
    with _apply_patches(s, market_open=True, extended_hours_enabled=True):
        r = await s["ar"]._route_one(_intent())

    assert len(s["broker_calls"]) == 1
    assert r["verdict"] == "executed"


@pytest.mark.asyncio
async def test_preflight_block_is_not_a_broker_error(route_one_scaffold):
    """The pre-flight block MUST NOT masquerade as a broker error.
    Doctrine: `broker_status=market_closed_preflight`, NOT
    `broker_error:market_closed`. The funnel error-metrics classifier
    keys on `broker_error:` prefix — a pre-flight block that
    smuggled that prefix would inflate broker-side error rates
    and hide the honest "market closed" signal from the operator."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire())
    s["risk_mod"].check = AsyncMock(return_value=_RiskOK())

    with _apply_patches(s, market_open=False):
        await s["ar"]._route_one(_intent())

    kwargs = s["executions_mod"].record.await_args.kwargs
    status = kwargs["broker_status"]
    # Positive: the pre-flight sentinel.
    assert status == "market_closed_preflight"
    # Negative: NOT the broker-error prefix.
    assert not status.startswith("broker_error:")


@pytest.mark.asyncio
async def test_preflight_writes_exactly_one_execution_row(route_one_scaffold):
    """One-row-per-attempt contract must survive the new gate. The
    denominator of the funnel (executions.count()) depends on it."""
    s = route_one_scaffold
    s["seat_mod"].decide = AsyncMock(return_value=_seat_fire())
    s["risk_mod"].check = AsyncMock(return_value=_RiskOK())

    with _apply_patches(s, market_open=False):
        await s["ar"]._route_one(_intent())

    assert s["executions_mod"].record.await_count == 1

