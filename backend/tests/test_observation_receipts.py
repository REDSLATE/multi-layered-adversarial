"""Tripwire tests for Observation Receipts (Phase 1 of ladder doctrine).

Doctrine pin (2026-02-18):
    Observation receipts are graded learning samples produced when:
      * intent action is directional (BUY/SELL/SHORT/COVER)
      * confidence ≥ 0.30
      * lane + symbol set
      * brain self-zeroed (size_multiplier == 0 OR
                          would_trade_without_gates == false)

    The receipt is SYNTHETIC (no broker, no money). It carries
    `eligible_for_learning=True` and `eligible_for_live_unlock=False`.
    The Phase 2 resolver will fetch market prices at +1h / +4h / +1d /
    +5d, compute outcomes, and grade. Phase 3 will count graded
    samples for ladder unlock decisions.
"""
from __future__ import annotations

import pytest


def _honest_hold_intent(**overrides):
    base = {
        "intent_id": "tw-obs-1",
        "stack": "camaro",
        "symbol": "BNB/USD",
        "action": "BUY",
        "lane": "crypto",
        "confidence": 0.677,
        "snapshot": {"spread_bps": 12.0, "price": 580.4},
        "evidence": {
            "raw_confidence": 0.1987,
            "size_multiplier": 0,
            "would_trade_without_gates": False,
            "conviction_tier": "MODERATE",
        },
    }
    base.update(overrides)
    return base


# ─── candidate classifier ─────────────────────────────────────────────


@pytest.mark.tripwire
def test_honest_hold_is_observation_candidate():
    """The exact prod-screenshot shape: BUY label, MODERATE tier,
    self-zeroed, with a real snapshot. MUST be a candidate."""
    from shared.observation_receipts import is_observation_candidate
    eligible, reason = is_observation_candidate(_honest_hold_intent())
    assert eligible is True
    assert "honest_hold" in reason


@pytest.mark.tripwire
def test_hold_action_not_observation_candidate():
    """HOLD intents never become trades and never become observations.
    HOLD is doctrinally separate from BUY/SELL even at zero size."""
    from shared.observation_receipts import is_observation_candidate
    eligible, reason = is_observation_candidate(
        _honest_hold_intent(action="HOLD"),
    )
    assert eligible is False
    assert reason == "not_directional"


@pytest.mark.tripwire
def test_below_confidence_floor_not_observation_candidate():
    """Conviction below 0.30 is noise — grading would pollute calibration."""
    from shared.observation_receipts import is_observation_candidate
    eligible, reason = is_observation_candidate(
        _honest_hold_intent(confidence=0.25),
    )
    assert eligible is False
    assert "confidence_below_floor" in reason


@pytest.mark.tripwire
def test_brain_sized_above_zero_not_observation_candidate():
    """When the brain actually sized up, the trade goes through the
    real-fill path. Observations are ONLY for self-zeroed honest holds."""
    from shared.observation_receipts import is_observation_candidate
    eligible, reason = is_observation_candidate(_honest_hold_intent(
        evidence={"size_multiplier": 0.5, "would_trade_without_gates": True},
    ))
    assert eligible is False
    assert reason == "brain_sized_above_zero"


@pytest.mark.tripwire
def test_missing_lane_or_symbol_not_observation_candidate():
    from shared.observation_receipts import is_observation_candidate
    eligible, _ = is_observation_candidate(_honest_hold_intent(lane=""))
    assert eligible is False
    eligible, _ = is_observation_candidate(_honest_hold_intent(symbol=""))
    assert eligible is False


# ─── receipt shape ────────────────────────────────────────────────────


@pytest.mark.tripwire
def test_receipt_shape_carries_doctrine_flags():
    """The persisted receipt MUST carry the typed doctrine flags so
    later phases (resolver, unlock counter) can filter cleanly."""
    from shared.observation_receipts import build_observation_receipt
    r = build_observation_receipt(_honest_hold_intent())
    assert r["receipt_type"] == "observation_fill"
    assert r["synthetic"] is True
    assert r["eligible_for_learning"] is True
    # Phase 1 doctrine: observations DO NOT count toward live unlock
    # (that's a Phase 3 concern with a separate counter / human gate).
    assert r["eligible_for_live_unlock"] is False
    assert r["resolved"] is False
    assert r["outcome"] is None


@pytest.mark.tripwire
def test_receipt_preserves_brain_honesty_telemetry():
    """The raw_confidence / size_multiplier / would_trade fields are
    the honesty signal — MUST round-trip into the receipt so the
    resolver can correlate calibration."""
    from shared.observation_receipts import build_observation_receipt
    r = build_observation_receipt(_honest_hold_intent())
    assert r["raw_confidence"] == 0.1987
    assert r["size_multiplier"] == 0
    assert r["would_trade_without_gates"] is False
    assert r["conviction_tier"] == "MODERATE"


@pytest.mark.tripwire
def test_receipt_anchors_price_from_snapshot():
    """The resolver needs a baseline anchor price to compute outcome
    at +1h/+4h/+1d/+5d. The receipt must anchor at observation time."""
    from shared.observation_receipts import build_observation_receipt
    r = build_observation_receipt(_honest_hold_intent())
    assert r["anchor_price"] == 580.4
    assert r["anchor_snapshot"]["spread_bps"] == 12.0


# ─── persistence + auto-router integration ────────────────────────────


@pytest.mark.tripwire
async def test_maybe_write_persists_when_eligible():
    """Eligible intent must produce a persisted row in the
    `observation_receipts` collection."""
    from db import db
    from namespaces import OBSERVATION_RECEIPTS
    from shared.observation_receipts import maybe_write_observation_receipt

    # Reset for hermetic test.
    await db[OBSERVATION_RECEIPTS].delete_many(
        {"intent_id": "tw-persist-1"},
    )
    intent = _honest_hold_intent(intent_id="tw-persist-1")
    receipt = await maybe_write_observation_receipt(intent)
    assert receipt is not None
    stored = await db[OBSERVATION_RECEIPTS].find_one(
        {"intent_id": "tw-persist-1"}, {"_id": 0},
    )
    assert stored is not None
    assert stored["eligible_for_learning"] is True


@pytest.mark.tripwire
async def test_maybe_write_returns_none_when_ineligible():
    """Ineligible intent must NOT produce a row (no silent pollution
    of the learning queue)."""
    from db import db
    from namespaces import OBSERVATION_RECEIPTS
    from shared.observation_receipts import maybe_write_observation_receipt

    intent = _honest_hold_intent(intent_id="tw-skip-1", action="HOLD")
    receipt = await maybe_write_observation_receipt(intent)
    assert receipt is None
    stored = await db[OBSERVATION_RECEIPTS].find_one(
        {"intent_id": "tw-skip-1"},
    )
    assert stored is None


# ─── routes ───────────────────────────────────────────────────────────


@pytest.mark.tripwire
def test_observation_routes_require_auth(base_url):
    import requests
    r = requests.get(f"{base_url}/api/admin/observation-receipts", timeout=15)
    assert r.status_code in (401, 403)
    r2 = requests.get(f"{base_url}/api/admin/observation-receipts/counts", timeout=15)
    assert r2.status_code in (401, 403)


@pytest.mark.tripwire
def test_counts_endpoint_shape(auth_client, base_url):
    r = auth_client.get(
        f"{base_url}/api/admin/observation-receipts/counts", timeout=15,
    )
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    assert "doctrine_note" in body
    # ladder threshold is locked at 100 (Phase 3 will read this).
    for it in body["items"]:
        assert it["ladder_unlock_threshold"] == 100
