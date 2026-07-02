"""Tests for the CFQS (Calibrated Fill Quality Score) merge-rights
doctrine — locked 2026-07-03 in /app/trader/merge_rights.py.

Split into two blocks:

    PURE / SUB-COMPONENT
        Direct tests of `compute_cfqs()` / `merge_right_ok()` — no
        store, no HTTP. Catches formula bugs early and lets a
        re-baseline session sanity-check its own math without
        spinning up the sidecar.

    ENDPOINT / GATE
        Tests the /brain-accuracy handler end-to-end through the
        SQLite fixture, verifying CFQS is attached, the gates
        behave at their boundaries, and cross-lane leakage is
        impossible.

Invariant note:
    Today, `confidence_n == fires` for every brain in the endpoint
    because `_seed_cycle` (and the live trader) always writes a
    numeric confidence on any fire. If that ever diverges — e.g.
    a HOLD-with-confidence receipt starts getting counted — the
    gate math needs a re-review.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")

from trader import audit, store   # noqa: E402
from trader.merge_rights import (   # noqa: E402
    BEAT_MARGIN,
    CFQSBreakdown,
    CONFIDENCE_N_FLOOR,
    FIRES_FLOOR,
    compute_cfqs,
    merge_right_ok,
)


# ─────────────────────────────────────────────────────────────────
# PURE / SUB-COMPONENT
# ─────────────────────────────────────────────────────────────────

def _qualified(**overrides) -> CFQSBreakdown:
    """Build a CFQS breakdown that qualifies on every axis, then
    let the caller override one field to test a specific edge."""
    base = dict(
        fires=30, fills=27, broker_errors=1,
        confidence_n=30,
        p50_quote_age_ms=200.0,
        avg_spread_bps=1.0,
        lane_median_spread_bps=1.0,
        confidence_p10=0.55, confidence_p90=0.75,   # spread 0.20 → 1.0
    )
    base.update(overrides)
    return compute_cfqs(**base)


def test_freshness_full_credit_under_500ms():
    b = _qualified(p50_quote_age_ms=200.0)
    assert b.freshness_factor == 1.0


def test_freshness_zero_at_5000ms():
    b = _qualified(p50_quote_age_ms=5000.0)
    assert b.freshness_factor == 0.0
    # And obviously the whole score collapses too.
    assert b.score == 0.0


def test_freshness_linear_decay_at_midpoint():
    """2750ms is the midpoint of the (500ms, 5000ms) decay window
    → expect ~0.5. Confirms linear, not a step function."""
    b = _qualified(p50_quote_age_ms=2750.0)
    assert b.freshness_factor == pytest.approx(0.5, abs=0.01)


def test_spread_penalty_full_credit_at_or_below_lane_median():
    b = _qualified(avg_spread_bps=1.0, lane_median_spread_bps=1.0)
    assert b.spread_penalty == 1.0
    b2 = _qualified(avg_spread_bps=0.5, lane_median_spread_bps=1.0)
    assert b2.spread_penalty == 1.0


def test_spread_penalty_halved_at_double_lane_median():
    b = _qualified(avg_spread_bps=2.0, lane_median_spread_bps=1.0)
    assert b.spread_penalty == pytest.approx(0.5, abs=0.001)


def test_calibration_penalty_full_below_ceiling():
    # spread 0.10 (well under 0.25 ceiling) → 1.0, untouched.
    b = _qualified(confidence_p10=0.60, confidence_p90=0.70)
    assert b.calibration_penalty == 1.0


def test_calibration_penalty_clamped_never_negative_never_over_one():
    # spread 0.40 → 1 - (0.40 - 0.25) * 4 = 1 - 0.60 = 0.40.
    b = _qualified(confidence_p10=0.30, confidence_p90=0.70)
    assert 0.0 <= b.calibration_penalty <= 1.0
    assert b.calibration_penalty == pytest.approx(0.40, abs=0.001)
    # Fully bimodal (spread 0.60) → 0.0, never negative.
    b2 = _qualified(confidence_p10=0.20, confidence_p90=0.80)
    assert b2.calibration_penalty == 0.0


# ─── gate boundaries ────────────────────────────────────────────

def test_gate_fires_exactly_at_floor_passes():
    b = _qualified(fires=FIRES_FLOOR, fills=FIRES_FLOOR, confidence_n=FIRES_FLOOR)
    assert b.fires_gate_passed is True
    assert b.merge_eligible is True


def test_gate_fires_one_below_floor_fails():
    """Off-by-one is the likely bug — 29 fires must NOT qualify."""
    b = _qualified(
        fires=FIRES_FLOOR - 1,
        fills=FIRES_FLOOR - 1,
        confidence_n=FIRES_FLOOR - 1,
    )
    assert b.fires_gate_passed is False
    assert b.merge_eligible is False


def test_gate_confidence_n_boundary():
    # fires OK, confidence_n one below floor → merge_eligible False.
    b = _qualified(confidence_n=CONFIDENCE_N_FLOOR - 1)
    assert b.confidence_gate_passed is False
    assert b.merge_eligible is False


def test_merge_right_exactly_at_beat_margin_denied():
    """Doctrine says candidate must BEAT incumbent by 15% — a tie at
    exactly 1.15x is NOT a beat. Strictly greater, not greater-or-equal."""
    incumbent = _qualified()  # score = fill_rate 0.9 * (1 - 1/30) ≈ 0.87
    # Force candidate to exactly incumbent.score * BEAT_MARGIN.
    target = incumbent.score * BEAT_MARGIN
    # Build a candidate whose score matches `target` by adjusting fills.
    # Since score is bounded by fill_rate here (all other factors = 1),
    # find the fills count that produces the target. We'll accept the
    # closest achievable and clamp; the important test is the equality
    # branch — so construct it directly as a breakdown.
    tied_candidate = CFQSBreakdown(
        score=round(target, 4),
        fill_rate=1.0, broker_error_rate=0.0,
        freshness_factor=1.0, spread_penalty=1.0, calibration_penalty=1.0,
        fires=30, confidence_n=30,
        fires_gate_passed=True, confidence_gate_passed=True,
        merge_eligible=True,
        p50_quote_age_ms=100.0, avg_spread_bps=1.0,
        lane_median_spread_bps=1.0,
        confidence_p10=0.6, confidence_p90=0.7,
    )
    ok, reason = merge_right_ok(tied_candidate, incumbent)
    assert ok is False
    assert "did not beat" in reason


def test_merge_right_strictly_above_beat_margin_allowed():
    incumbent = _qualified()
    winning = CFQSBreakdown(
        score=round(incumbent.score * BEAT_MARGIN + 0.001, 4),
        fill_rate=1.0, broker_error_rate=0.0,
        freshness_factor=1.0, spread_penalty=1.0, calibration_penalty=1.0,
        fires=30, confidence_n=30,
        fires_gate_passed=True, confidence_gate_passed=True,
        merge_eligible=True,
        p50_quote_age_ms=100.0, avg_spread_bps=1.0,
        lane_median_spread_bps=1.0,
        confidence_p10=0.6, confidence_p90=0.7,
    )
    ok, _ = merge_right_ok(winning, incumbent)
    assert ok is True


def test_merge_right_denies_candidate_missing_gates():
    incumbent = _qualified()
    understaffed = _qualified(fires=10, fills=9, confidence_n=10)
    ok, reason = merge_right_ok(understaffed, incumbent)
    assert ok is False
    assert reason == "candidate_gates_not_met"


# ─────────────────────────────────────────────────────────────────
# ENDPOINT / GATE — via the SQLite fixture
# ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def fresh_store(tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    store.init(
        str(tmp_path / "executions.sqlite"),
        str(tmp_path / "jsonl"),
    )
    yield tmp_path
    store.close()
    loop.close()
    asyncio.set_event_loop(None)


async def _seed_fire(
    *,
    cycle_id: str, brain: str, lane: str, confidence: float,
    spread_bps: float, quote_age_ms: float,
    filled: bool = True, ts: str = "2026-07-03T12:00:00+00:00",
):
    """Seed one fire receipt + matching execution row.

    Note: `confidence_n` in the endpoint equals `fires` today because
    every fire carries a numeric confidence (see test-module docstring).
    """
    await audit.write_receipt(
        db=None,
        cycle_id=cycle_id, lane=lane, symbol="TSLA" if lane == "equity" else "BTCUSD",
        last_price=100.0,
        signals=[{"brain": brain, "verdict": "BUY", "confidence": confidence}],
        chosen={"brain": brain, "verdict": "BUY", "confidence": confidence},
        seats={"executor": brain},
        angels={"executor": "test"},
        risk_verdict={"ok": True, "reason": "ok"},
        quote={
            "quote_source": "webull_mqtt",
            "quote_age_ms": quote_age_ms,
            "spread_bps": spread_bps,
            "bid": 100.0, "ask": 100.0 + spread_bps / 100,
            "last_price": 100.0, "l1_stale": False,
        },
    )
    with store._lock:
        store._require_conn().execute(
            "UPDATE trader_receipts SET ts=? WHERE cycle_id=?",
            (ts, cycle_id),
        )
    store.record_execution({
        "intent_id": f"trader-{cycle_id[:16]}-{lane}",
        "ts": ts,
        "brain": brain, "lane": lane, "action": "BUY",
        "symbol": "TSLA" if lane == "equity" else "BTCUSD",
        "notional_usd": 5.0,
        "ok": filled, "broker": "webull",
        "broker_order_id": f"ord-{cycle_id}" if filled else None,
        "exception_type": None if filled else "BrokerError",
    })


@pytest.mark.asyncio
async def test_endpoint_attaches_cfqs_block(fresh_store):
    """Basic shape test — every brain in the response gets a `cfqs`
    dict with the sub-factors we lock into the doctrine."""
    from backend.routes import admin_trader
    admin_trader._import_trader()

    # 30 clean fires — right at the floor.
    for i in range(FIRES_FLOOR):
        await _seed_fire(
            cycle_id=f"c-{i:03d}",
            brain="camino", lane="equity",
            confidence=0.65, spread_bps=1.0, quote_age_ms=200.0,
        )
    result = await admin_trader.trader_brain_accuracy(
        _={}, window_hours=24, lane=None,
    )
    cam = next(b for b in result["brains"] if b["brain"] == "camino")
    cfqs = cam["cfqs"]
    for k in (
        "score", "fill_rate", "broker_error_rate", "freshness_factor",
        "spread_penalty", "calibration_penalty",
        "fires_gate_passed", "confidence_gate_passed", "merge_eligible",
    ):
        assert k in cfqs, f"CFQS missing key {k}"
    assert cam["confidence_n"] == FIRES_FLOOR
    assert cfqs["merge_eligible"] is True


@pytest.mark.asyncio
async def test_endpoint_below_floor_not_merge_eligible(fresh_store):
    """29 fires — one under the floor. CFQS attaches but merge_eligible
    must be False. This is the off-by-one guard."""
    from backend.routes import admin_trader
    admin_trader._import_trader()

    for i in range(FIRES_FLOOR - 1):
        await _seed_fire(
            cycle_id=f"c-{i:03d}",
            brain="barracuda", lane="equity",
            confidence=0.70, spread_bps=1.0, quote_age_ms=200.0,
        )
    result = await admin_trader.trader_brain_accuracy(
        _={}, window_hours=24, lane=None,
    )
    bar = next(b for b in result["brains"] if b["brain"] == "barracuda")
    assert bar["fires"] == FIRES_FLOOR - 1
    assert bar["cfqs"]["fires_gate_passed"] is False
    assert bar["cfqs"]["merge_eligible"] is False


@pytest.mark.asyncio
async def test_endpoint_no_fires_brain_absent_not_zero(fresh_store):
    """A brain with zero fires in the window must NOT appear in the
    output — absence, not a zero-filled row. Mirrors the HOLD test."""
    from backend.routes import admin_trader
    admin_trader._import_trader()

    # Only Camino fires; Barracuda is silent.
    await _seed_fire(
        cycle_id="c-only",
        brain="camino", lane="equity",
        confidence=0.7, spread_bps=1.0, quote_age_ms=200.0,
    )
    result = await admin_trader.trader_brain_accuracy(
        _={}, window_hours=24, lane=None,
    )
    brains = [b["brain"] for b in result["brains"]]
    assert "camino" in brains
    assert "barracuda" not in brains


@pytest.mark.asyncio
async def test_endpoint_lane_filter_prevents_cross_lane_median(fresh_store):
    """Doctrine: crypto's regime shape ≠ equity's, so the lane-median
    spread must be computed WITHIN a lane. When the endpoint is called
    with `lane='equity'`, a crypto brain's spread must not pollute
    the equity median."""
    from backend.routes import admin_trader
    admin_trader._import_trader()

    # Equity brain: tight spread (1 bps)
    for i in range(FIRES_FLOOR):
        await _seed_fire(
            cycle_id=f"eq-{i:03d}",
            brain="camino", lane="equity",
            confidence=0.7, spread_bps=1.0, quote_age_ms=200.0,
        )
    # Crypto brain: wide spread (20 bps) — must NOT skew equity median.
    for i in range(FIRES_FLOOR):
        await _seed_fire(
            cycle_id=f"cr-{i:03d}",
            brain="hellcat", lane="crypto",
            confidence=0.7, spread_bps=20.0, quote_age_ms=200.0,
        )
    result = await admin_trader.trader_brain_accuracy(
        _={}, window_hours=24, lane="equity",
    )
    assert result["lane_median_spread_bps"] == pytest.approx(1.0, abs=0.01)
    # Only equity brains present.
    assert all(
        b["brain"] == "camino" for b in result["brains"]
    ), "crypto brain leaked into equity-filtered response"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
