"""Tripwires for the Phase A R:R gate (2026-05-27).

Doctrine pinned:
  * Equity entries (BUY/SHORT) face a 3:1 reward-to-risk floor.
  * Phase A: missing target_price / stop_price → soft-pass with typed
    warning reason (brains have a rollout window).
  * Hard rejects (regardless of phase):
      - RR_RATIO_BELOW_FLOOR (ratio < rr_min)
      - RR_INVALID_PRICES (direction-incoherent)
  * Crypto + exit verbs (SELL/COVER) skip the gate cleanly.
  * Reason strings are audit-stable — pinned here so a rename is a
    deliberate doctrine event, not an accident.

These tests are pure-function tests against `shared.rr_gate.evaluate_rr`.
No HTTP, no DB — the gate's audit shape is independent of where it's
called from.
"""
from __future__ import annotations

import os

import pytest

from shared.rr_gate import RR_RATIO_MIN_EQUITY, evaluate_rr, reload_env


def _intent(
    *,
    lane="equity", action="BUY",
    target=120.0, stop=95.0, entry=100.0,
    snapshot_extra: dict | None = None,
):
    snap = {"price": entry}
    if snapshot_extra:
        snap.update(snapshot_extra)
    return {
        "lane": lane,
        "action": action,
        "target_price": target,
        "stop_price": stop,
        "snapshot": snap,
    }


@pytest.mark.tripwire
def test_rr_default_floor_is_three_to_one():
    """Operator-locked: the default floor is 3:1, not 5:1 or 1:1."""
    assert RR_RATIO_MIN_EQUITY == 3.0


@pytest.mark.tripwire
def test_rr_buy_passes_at_exact_3to1():
    # entry=100, target=130 → reward=30; stop=90 → risk=10. ratio=3.0.
    intent = _intent(target=130.0, stop=90.0, entry=100.0)
    d = evaluate_rr(intent)
    assert d.passed
    assert d.reason == "RR_RATIO_OK"
    assert d.rr_ratio == pytest.approx(3.0)
    assert d.direction == "long"


@pytest.mark.tripwire
def test_rr_buy_fails_below_floor():
    # entry=100, target=115 → reward=15; stop=90 → risk=10. ratio=1.5.
    intent = _intent(target=115.0, stop=90.0, entry=100.0)
    d = evaluate_rr(intent)
    assert not d.passed
    assert d.reason == "RR_RATIO_BELOW_FLOOR"
    assert d.rr_ratio == pytest.approx(1.5)


@pytest.mark.tripwire
def test_rr_short_passes_at_3to1():
    # SHORT: entry=100, target=70 → reward=30; stop=110 → risk=10. ratio=3.0
    intent = _intent(action="SHORT", target=70.0, stop=110.0, entry=100.0)
    d = evaluate_rr(intent)
    assert d.passed
    assert d.reason == "RR_RATIO_OK"
    assert d.rr_ratio == pytest.approx(3.0)
    assert d.direction == "short"


@pytest.mark.tripwire
def test_rr_short_fails_below_floor():
    # SHORT entry=100, target=95 → reward=5; stop=105 → risk=5. ratio=1.0
    intent = _intent(action="SHORT", target=95.0, stop=105.0, entry=100.0)
    d = evaluate_rr(intent)
    assert not d.passed
    assert d.reason == "RR_RATIO_BELOW_FLOOR"
    assert d.rr_ratio == pytest.approx(1.0)


@pytest.mark.tripwire
def test_rr_invalid_prices_buy_target_below_entry():
    """A BUY with target below entry is direction-incoherent — HARD
    REJECT even in Phase A. Not a 'maybe later' configuration gap."""
    intent = _intent(action="BUY", target=90.0, stop=80.0, entry=100.0)
    d = evaluate_rr(intent)
    assert not d.passed
    assert d.reason == "RR_INVALID_PRICES"


@pytest.mark.tripwire
def test_rr_invalid_prices_short_target_above_entry():
    intent = _intent(action="SHORT", target=110.0, stop=120.0, entry=100.0)
    d = evaluate_rr(intent)
    assert not d.passed
    assert d.reason == "RR_INVALID_PRICES"


@pytest.mark.tripwire
def test_rr_invalid_prices_buy_stop_above_entry():
    # BUY with stop above entry is incoherent regardless of target placement.
    intent = _intent(action="BUY", target=130.0, stop=110.0, entry=100.0)
    d = evaluate_rr(intent)
    assert not d.passed
    assert d.reason == "RR_INVALID_PRICES"


# ─── Phase A soft-pass for missing fields ─────────────────────────────


@pytest.mark.tripwire
def test_rr_phase_a_missing_target_soft_passes():
    intent = {"lane": "equity", "action": "BUY", "target_price": None,
              "stop_price": 90.0, "snapshot": {"price": 100.0}}
    d = evaluate_rr(intent)
    assert d.passed, "Phase A must soft-pass missing target"
    assert d.reason == "RR_MISSING_TARGET_OR_STOP"
    assert d.phase_a_soft is True


@pytest.mark.tripwire
def test_rr_phase_a_missing_stop_soft_passes():
    intent = {"lane": "equity", "action": "BUY", "target_price": 130.0,
              "stop_price": None, "snapshot": {"price": 100.0}}
    d = evaluate_rr(intent)
    assert d.passed
    assert d.reason == "RR_MISSING_TARGET_OR_STOP"


@pytest.mark.tripwire
def test_rr_phase_a_missing_entry_soft_passes():
    intent = {"lane": "equity", "action": "BUY", "target_price": 130.0,
              "stop_price": 90.0, "snapshot": {}}
    d = evaluate_rr(intent)
    assert d.passed
    assert d.reason == "RR_MISSING_ENTRY_PRICE"


@pytest.mark.tripwire
def test_rr_phase_b_hard_required_flips_to_reject(monkeypatch):
    """When `RR_REQUIRE_FIELDS_HARD=true`, missing fields become a
    HARD REJECT (Phase B). Reason stays the same; only `passed` flips."""
    monkeypatch.setenv("RR_REQUIRE_FIELDS_HARD", "true")
    reload_env()
    try:
        intent = {"lane": "equity", "action": "BUY", "target_price": None,
                  "stop_price": 90.0, "snapshot": {"price": 100.0}}
        d = evaluate_rr(intent)
        assert not d.passed
        assert d.reason == "RR_MISSING_TARGET_OR_STOP"
    finally:
        monkeypatch.setenv("RR_REQUIRE_FIELDS_HARD", "false")
        reload_env()


# ─── Out-of-scope intents (clean pass-through) ────────────────────────


@pytest.mark.tripwire
def test_rr_crypto_lane_skipped():
    intent = _intent(lane="crypto", target=101.0, stop=99.0, entry=100.0)
    d = evaluate_rr(intent)
    # crypto skipped EVEN THOUGH the ratio (0.5) is well below 3:1.
    assert d.passed
    assert d.reason == "RR_NOT_APPLICABLE_LANE"


@pytest.mark.tripwire
@pytest.mark.parametrize("action", ["SELL", "COVER", "HOLD"])
def test_rr_exit_verbs_skipped(action):
    intent = _intent(action=action, target=80.0, stop=120.0, entry=100.0)
    d = evaluate_rr(intent)
    assert d.passed
    assert d.reason == "RR_NOT_APPLICABLE_ACTION"


# ─── Env-tunable floor ────────────────────────────────────────────────


@pytest.mark.tripwire
def test_rr_floor_is_env_tunable(monkeypatch):
    """Operator can tighten the floor via env without a redeploy."""
    monkeypatch.setenv("RR_RATIO_MIN_EQUITY", "5.0")
    reload_env()
    try:
        # 3:1 setup now FAILS against the 5:1 tightened floor.
        intent = _intent(target=130.0, stop=90.0, entry=100.0)
        d = evaluate_rr(intent, rr_min=5.0)
        assert not d.passed
        assert d.reason == "RR_RATIO_BELOW_FLOOR"
        assert d.rr_min == 5.0
    finally:
        monkeypatch.setenv("RR_RATIO_MIN_EQUITY", "3.0")
        reload_env()


# ─── Audit-stable reason vocabulary ───────────────────────────────────


@pytest.mark.tripwire
def test_rr_reason_vocabulary_pinned():
    """The set of possible reason strings is pinned. Adding a new reason
    is fine; renaming an existing one is a doctrine event that must
    update downstream consumers (audit dashboards, MC Shelly, training
    label scorers). This test refuses silent drift."""
    expected = {
        "RR_RATIO_OK",
        "RR_NOT_APPLICABLE_LANE",
        "RR_NOT_APPLICABLE_ACTION",
        "RR_MISSING_TARGET_OR_STOP",
        "RR_MISSING_ENTRY_PRICE",
        "RR_INVALID_PRICES",
        "RR_RATIO_BELOW_FLOOR",
    }
    # Exercise every branch and collect the reasons we actually emit.
    reasons: set[str] = set()
    reasons.add(evaluate_rr(_intent(target=130, stop=90, entry=100)).reason)
    reasons.add(evaluate_rr(_intent(lane="crypto")).reason)
    reasons.add(evaluate_rr(_intent(action="SELL")).reason)
    reasons.add(evaluate_rr({"lane": "equity", "action": "BUY",
                             "target_price": None, "stop_price": 90,
                             "snapshot": {"price": 100}}).reason)
    reasons.add(evaluate_rr({"lane": "equity", "action": "BUY",
                             "target_price": 130, "stop_price": 90,
                             "snapshot": {}}).reason)
    reasons.add(evaluate_rr(_intent(target=90, stop=80, entry=100)).reason)
    reasons.add(evaluate_rr(_intent(target=115, stop=90, entry=100)).reason)
    assert reasons == expected, (
        f"R:R reason vocabulary drifted. Expected {expected}, got {reasons}"
    )
