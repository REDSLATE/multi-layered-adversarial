"""Broker-error taxonomy + terminal-block doctrine (2026-02-17).

Root cause of the 2026-02-17 pending-intent pileup: the auto-router's
generic broker-exception handler recorded the failure to `executions`
but explicitly did NOT stamp the intent as terminal, on the assumption
that "broker errors are transient". On Sunday that assumption fails
loudly — market_closed + insufficient_funds + min_order_notional all
return deterministic errors that retry forever, head-of-lining the
tick queue against fresh post-fix intents.

Doctrine locked in by these tests:

    TERMINAL — stamp the intent `gate_state=blocked` on FIRST failure:
        market_closed, insufficient_funds, min_order_notional,
        invalid_order_args, auth_or_permission

    TRANSIENT — increment `broker_retry_count`; terminate at cap
    (default AUTO_ROUTER_MAX_BROKER_RETRIES=5) with reason
    `broker_retry_exhausted`:
        rate_limited, network_transient, unknown

Precedence matters — Kraken's `EGeneral:Invalid arguments:volume
minimum not met` contains BOTH "invalid arguments" and "volume
minimum". Must be classified as `min_order_notional`, not
`invalid_order_args`, because the FIX is different (increase order
size vs debug the request format).
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")


# ─── Real-world exception message samples ───────────────────────────
# Verbatim strings pulled from `/var/log/supervisor/backend.err.log`
# during the 2026-02-17 investigation. Do NOT change unless the
# broker CHANGES its wire format — these are our regression anchors.
WEBULL_SUNDAY = (
    "Webull submit_market_order (v2) failed: HTTP Status: 417, Code: "
    "INVALID_PARAMETER, Msg: The time you sent is not supported. "
    "Please check. , RequestID: e0be6a27-072c-4ebc-a837-9490bc3f9a9f"
)
KRAKEN_NO_FUNDS = "EOrder:Insufficient funds"
KRAKEN_MIN_VOL  = "EGeneral:Invalid arguments:volume minimum not met"
KRAKEN_RATE     = "EAPI:Rate limit exceeded"
NETWORK_TIMEOUT = "HTTPSConnectionPool: Read timed out (read timeout=30)"
UNKNOWN_BLOB    = "Some novel broker error we've never seen before, 0xdeadbeef"


# ─── Taxonomy: bucket assignment ────────────────────────────────────

def test_classify_market_closed_beats_invalid_args_on_webull_sunday():
    """Precedence guard: Webull's Sunday error has BOTH 'INVALID_PARAMETER'
    and 'not supported'. Must classify as market_closed — invalid_order_args
    would misroute the operator to fix the request format when the actual
    fix is to wait for RTH."""
    from shared.broker_error_taxonomy import classify
    r = classify(RuntimeError(WEBULL_SUNDAY))
    assert r.bucket == "market_closed"
    assert r.is_terminal is True


def test_classify_min_order_notional_beats_invalid_args_on_kraken():
    """Kraken's response reads 'EGeneral:Invalid arguments:volume
    minimum not met' — the specific fix is `increase size`, not
    `debug arg shape`. Must classify as min_order_notional."""
    from shared.broker_error_taxonomy import classify
    r = classify(RuntimeError(KRAKEN_MIN_VOL))
    assert r.bucket == "min_order_notional"
    assert r.is_terminal is True


def test_classify_insufficient_funds_terminal():
    from shared.broker_error_taxonomy import classify
    r = classify(RuntimeError(KRAKEN_NO_FUNDS))
    assert r.bucket == "insufficient_funds"
    assert r.is_terminal is True


def test_classify_rate_limited_transient():
    """Rate limit IS transient — retrying after a moment usually works."""
    from shared.broker_error_taxonomy import classify
    r = classify(RuntimeError(KRAKEN_RATE))
    assert r.bucket == "rate_limited"
    assert r.is_terminal is False


# Regression anchor (2026-07-06):
# The pre-fix ordering had `invalid_order_args` before `rate_limited`.
# Webull's real 429 response reads:
#   "HTTP Status: 429, Code: TOO_MANY_REQUESTS, Msg: Too many requests"
# which contains "http status: 4" — the 4xx catch-all in
# `invalid_order_args` greedily grabbed it and misclassified as
# TERMINAL. That defeated the retry cap for the single most likely
# broker rejection during RTH volume (rate-limit throttling). The
# fix reorders the classify() function to peel `rate_limited` off
# BEFORE the generic 4xx clause. This test locks that ordering.
WEBULL_429 = (
    "Webull submit_market_order failed: HTTP Status: 429, "
    "Code: TOO_MANY_REQUESTS, Msg: Too many requests, please retry later."
)


def test_classify_webull_429_transient():
    """Webull's REAL 429 wire format contains 'http status: 4' — must
    still classify as rate_limited (transient), NOT invalid_order_args."""
    from shared.broker_error_taxonomy import classify
    r = classify(RuntimeError(WEBULL_429))
    assert r.bucket == "rate_limited", (
        f"Webull 429 misclassified as {r.bucket!r}. The 4xx catch-all "
        "in invalid_order_args must NOT peel off before rate_limited."
    )
    assert r.is_terminal is False


def test_classify_network_timeout_transient():
    from shared.broker_error_taxonomy import classify
    r = classify(RuntimeError(NETWORK_TIMEOUT))
    assert r.bucket == "network_transient"
    assert r.is_terminal is False


def test_classify_unknown_defaults_to_transient():
    """Safe default: an unrecognized error retries a few times before
    being terminated. Better to double-attempt a novel transient than
    to permanently drop a novel-but-actually-recoverable class."""
    from shared.broker_error_taxonomy import classify
    r = classify(RuntimeError(UNKNOWN_BLOB))
    assert r.bucket == "unknown"
    assert r.is_terminal is False


def test_classify_auth_error_terminal():
    from shared.broker_error_taxonomy import classify
    r = classify(RuntimeError("HTTP 401 Unauthorized"))
    assert r.bucket == "auth_or_permission"
    assert r.is_terminal is True


def test_classify_detail_is_bounded():
    """Detail stored on the intent must not exceed ~120 chars — bigger
    payloads bloat the intent doc + logs."""
    from shared.broker_error_taxonomy import classify
    r = classify(RuntimeError("x" * 5000))
    assert len(r.detail) <= 120


def test_bucket_sets_are_disjoint_and_named_correctly():
    """The two families must never share a bucket — the union names
    the entire vocabulary. If someone renames a bucket without
    updating the constants, this catches it."""
    from shared.broker_error_taxonomy import TERMINAL_BUCKETS, TRANSIENT_BUCKETS
    assert TERMINAL_BUCKETS.isdisjoint(TRANSIENT_BUCKETS)
    expected_terminal = {
        "market_closed", "insufficient_funds", "min_order_notional",
        "invalid_order_args", "auth_or_permission",
    }
    expected_transient = {"rate_limited", "network_transient", "unknown"}
    assert TERMINAL_BUCKETS == expected_terminal
    assert TRANSIENT_BUCKETS == expected_transient


# ─── Auto-router terminal-block behavior ────────────────────────────


@pytest.fixture
def route_one_setup():
    """Common patch scaffold — Seat fires, Risk clears, Broker raises."""
    from shared import auto_router as ar

    async def fake_seat_decide(_intent):
        return _seat_decision_fire()

    class _RiskOK:
        ok = True
        notional_usd = 10.0
        reason = "ok"

    async def fake_risk_check(_intent, notional_usd):  # noqa: ARG001
        return _RiskOK()

    updated_docs: list[dict] = []
    coll = MagicMock()

    async def _update_one(query, update):
        # Simulate the driver's return, but also capture the write for
        # assertion.
        updated_docs.append({"query": query, "update": update})

    coll.update_one = _update_one
    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(return_value=coll)

    seat_mod = MagicMock()
    seat_mod.decide = fake_seat_decide

    risk_mod = MagicMock()
    risk_mod.check = fake_risk_check
    # 2026-02-28: the cap-authority guard reads `risk.per_order_cap()`.
    # Default it high so it never conflicts with the pair-floor size-up
    # (these tests focus on broker error taxonomy, not cap-vs-floor).
    risk_mod.per_order_cap = MagicMock(return_value=10000.0)

    executions_mod = MagicMock()
    executions_mod.record = AsyncMock()

    return {
        "ar": ar,
        "fake_db": fake_db,
        "updated_docs": updated_docs,
        "seat_mod": seat_mod,
        "risk_mod": risk_mod,
        "executions_mod": executions_mod,
    }


def _seat_decision_fire():
    """Minimal seat-decision stand-in that behaves like a real
    `SeatDecision` for the fields `_route_one` reads. We use
    `SimpleNamespace` so nothing resolves to a MagicMock through the
    patched `shared.seat` module (which is what happens when this
    helper imports `SeatDecision` from a mocked module).
    """
    from types import SimpleNamespace
    return SimpleNamespace(
        verdict="fire",
        reason="executor_self_fires",
        strategist="barracuda",
        governor="hellcat",
        executor="camino",
        auditor="gto",
        angels={"strategist": "Raziel", "governor": "Nuriel",
                "executor": "Paschar", "auditor": "Sariel"},
        risk_multiplier=1.0,
        lane="crypto",
        intent_brain="camino",
    )


def _intent(**overrides):
    base = {
        "intent_id": "test-intent-1",
        "symbol": "BTC/USD",
        "action": "BUY",
        "lane": "crypto",
        "stack": "camino",
        "ingest_ts": "2026-02-17T00:00:00+00:00",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_terminal_broker_error_stamps_intent_immediately(route_one_setup):
    """Sunday's market_closed error MUST stamp `gate_state=blocked`
    on the FIRST attempt so it exits the tick queue."""
    s = route_one_setup
    ar = s["ar"]

    async def broker_raises(*_a, **_k):
        raise RuntimeError(WEBULL_SUNDAY)

    with patch.dict(sys.modules, {
        "shared.seat": s["seat_mod"],
        "shared.risk": s["risk_mod"],
        "shared.executions": s["executions_mod"],
    }), \
         patch.object(ar, "db", s["fake_db"]), \
         patch.object(ar, "_is_master_switch_armed",
                      new=AsyncMock(return_value=True), create=True), \
         patch("shared.broker_router.route_order", broker_raises, create=True):
        r = await ar._route_one(_intent())

    assert r["verdict"] == "blocked"
    assert r["reason"] == "market_closed"
    # The intent must be terminally stamped:
    writes = [d for d in s["updated_docs"]
              if d["update"].get("$set", {}).get("gate_state") == "blocked"]
    assert len(writes) == 1, f"Expected 1 terminal stamp, got: {s['updated_docs']}"
    w = writes[0]["update"]["$set"]
    assert w["broker_reason"] == "market_closed"
    assert w["broker_error_bucket"] == "market_closed"


@pytest.mark.asyncio
async def test_transient_broker_error_only_increments_retry_count(route_one_setup):
    """A single network timeout should NOT terminate the intent —
    just bump the retry counter for the next tick to try again."""
    s = route_one_setup
    ar = s["ar"]

    async def broker_raises(*_a, **_k):
        raise RuntimeError(NETWORK_TIMEOUT)

    with patch.dict(sys.modules, {
        "shared.seat": s["seat_mod"],
        "shared.risk": s["risk_mod"],
        "shared.executions": s["executions_mod"],
    }), \
         patch.object(ar, "db", s["fake_db"]), \
         patch.object(ar, "_is_master_switch_armed",
                      new=AsyncMock(return_value=True), create=True), \
         patch("shared.broker_router.route_order", broker_raises, create=True):
        r = await ar._route_one(_intent(broker_retry_count=0))

    assert r["verdict"] == "error"
    assert r["broker_error_bucket"] == "network_transient"
    # No terminal stamp:
    terminals = [d for d in s["updated_docs"]
                 if d["update"].get("$set", {}).get("gate_state") == "blocked"]
    assert not terminals, "Transient error should NOT have terminated the intent"
    # Retry counter must have incremented:
    incs = [d for d in s["updated_docs"] if "$inc" in d["update"]]
    assert len(incs) == 1
    assert incs[0]["update"]["$inc"] == {"broker_retry_count": 1}


@pytest.mark.asyncio
async def test_transient_broker_error_terminates_at_retry_cap(route_one_setup):
    """Once `broker_retry_count` reaches `AUTO_ROUTER_MAX_BROKER_RETRIES-1`
    (=4 with default cap 5), the NEXT transient error must terminate
    the intent with `broker_retry_exhausted`."""
    s = route_one_setup
    ar = s["ar"]

    async def broker_raises(*_a, **_k):
        raise RuntimeError(NETWORK_TIMEOUT)

    # retry_count_before = 4; increment → 5 → cap reached → terminate.
    with patch.dict(sys.modules, {
        "shared.seat": s["seat_mod"],
        "shared.risk": s["risk_mod"],
        "shared.executions": s["executions_mod"],
    }), \
         patch.object(ar, "db", s["fake_db"]), \
         patch.object(ar, "_is_master_switch_armed",
                      new=AsyncMock(return_value=True), create=True), \
         patch.object(ar, "AUTO_ROUTER_MAX_BROKER_RETRIES", 5), \
         patch("shared.broker_router.route_order", broker_raises, create=True):
        r = await ar._route_one(_intent(broker_retry_count=4))

    assert r["verdict"] == "blocked"
    assert r["reason"] == "broker_retry_exhausted"
    terminals = [d for d in s["updated_docs"]
                 if d["update"].get("$set", {}).get("gate_state") == "blocked"]
    assert len(terminals) == 1
    assert terminals[0]["update"]["$set"]["broker_reason"] == "broker_retry_exhausted"
    # Bucket is preserved (still network_transient) so the funnel can
    # attribute this class of exhaustion correctly downstream.
    assert terminals[0]["update"]["$set"]["broker_error_bucket"] == "network_transient"


@pytest.mark.asyncio
async def test_min_notional_terminates_immediately_not_after_retries(route_one_setup):
    """Kraken's volume-minimum-not-met is DETERMINISTIC. Retrying the
    same order size 5 times will fail 5 times — must terminate on the
    first attempt, not after 5."""
    s = route_one_setup
    ar = s["ar"]

    async def broker_raises(*_a, **_k):
        raise RuntimeError(KRAKEN_MIN_VOL)

    with patch.dict(sys.modules, {
        "shared.seat": s["seat_mod"],
        "shared.risk": s["risk_mod"],
        "shared.executions": s["executions_mod"],
    }), \
         patch.object(ar, "db", s["fake_db"]), \
         patch.object(ar, "_is_master_switch_armed",
                      new=AsyncMock(return_value=True), create=True), \
         patch("shared.broker_router.route_order", broker_raises, create=True):
        r = await ar._route_one(_intent(broker_retry_count=0))

    assert r["verdict"] == "blocked"
    assert r["reason"] == "min_order_notional"
    incs = [d for d in s["updated_docs"] if "$inc" in d["update"]]
    assert not incs, "Terminal errors must NOT increment the retry counter"
