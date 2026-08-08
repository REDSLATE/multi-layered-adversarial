"""Ignition Watch + Missed-Entry Ledger tests (2026-08-04)."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from momentum.ignition_watch import compute_candidates  # noqa: E402
from shared.risk_sizer.missed_entries import (  # noqa: E402
    block_price, classify_outcome, in_scope,
)

pytestmark = pytest.mark.tripwire


# ── ignition ranking (pure) ─────────────────────────────────────────

def test_ignition_ranks_by_volume_rate_and_caps_top_n():
    prev = {"ICNT": (1_000_000.0, 1.00), "SLOW": (5_000_000.0, 2.00),
            "FAST": (2_000_000.0, 0.50)}
    rows = {"ICNT": (1_060_000.0, 1.05),   # +$60k/min, +5%
            "SLOW": (5_005_000.0, 2.01),   # +$5k/min — below floor
            "FAST": (2_030_000.0, 0.52)}   # +$30k/min, +4%
    out = compute_candidates(rows, prev, 1.0, top_n=2,
                             min_vol_usd_min=10_000.0)
    assert [c["symbol"] for c in out] == ["ICNT/USD", "FAST/USD"]
    assert out[0]["vol_rate_usd_min"] == 60_000
    assert out[0]["price_change_pct"] == 5.0


def test_ignition_requires_positive_price_move():
    prev = {"DUMP": (1_000_000.0, 1.00)}
    rows = {"DUMP": (1_500_000.0, 0.90)}  # huge volume, red price
    assert compute_candidates(rows, prev, 1.0, top_n=5,
                              min_vol_usd_min=10_000.0) == []


def test_ignition_needs_baseline_and_scales_elapsed():
    rows = {"NEW": (1_000_000.0, 1.0)}
    assert compute_candidates(rows, {}, 1.0, top_n=5,
                              min_vol_usd_min=1.0) == []
    prev = {"X": (1_000_000.0, 1.00)}
    cur = {"X": (1_060_000.0, 1.02)}
    out = compute_candidates(cur, prev, 2.0, top_n=5,
                             min_vol_usd_min=10_000.0)
    assert out[0]["vol_rate_usd_min"] == 30_000  # $60k over 2 min


# ── missed-entry scope + block price ────────────────────────────────

def test_in_scope_opportunity_gates_only():
    assert in_scope("entry_timing:MISSED_ENTRY_CHASE_RISK")
    assert in_scope("risk_sizer:not_in_buy_allowlist")
    assert in_scope("risk_sizer:below_volume_floor")
    assert in_scope("risk_sizer:post_sell_cooldown")
    assert not in_scope("risk_sizer:sized_to_zero")
    # 2026-08-08 operator decision: funds-blocked signals now count
    assert in_scope("risk_sizer:no_balance_no_trade")
    assert in_scope("risk_sizer:insufficient_balance")
    assert not in_scope("seat_did_not_fire")
    assert not in_scope(None)


def test_block_price_derivation_ladder():
    assert block_price({"entry_timing_receipt":
                        {"confirmation_price": 1.30}}) == 1.30
    assert block_price({"snapshot": {"bid": 1.0, "ask": 1.1}}) == pytest.approx(1.05)
    assert block_price({"price_at_signal": 2.5}) == 2.5
    assert block_price({}) is None


# ── counterfactual outcome math (pure) ──────────────────────────────

def _bar(h, l, c):
    return {"h": h, "l": l, "c": c}


def test_counterfactual_tp_first_touch():
    v = classify_outcome(100.0, [
        _bar(102, 99, 101), _bar(106, 101, 105), _bar(104, 95, 96),
    ], tp_pct=5.0, sl_pct=3.0)
    assert v["outcome"] == "tp_hit" and not v["ambiguous"]
    assert v["peak_pct"] == 6.0          # full-horizon peak
    assert v["trough_pct"] == -5.0       # full-horizon trough
    assert v["end_pct"] == -4.0


def test_counterfactual_sl_first_touch():
    v = classify_outcome(100.0, [
        _bar(101, 96.5, 97), _bar(108, 97, 107),
    ], tp_pct=5.0, sl_pct=3.0)
    assert v["outcome"] == "sl_hit"      # sl at 97 touched in bar 1


def test_counterfactual_ambiguous_bar_is_conservative():
    v = classify_outcome(100.0, [_bar(106, 96, 100)],
                         tp_pct=5.0, sl_pct=3.0)
    assert v["outcome"] == "sl_hit" and v["ambiguous"]


def test_counterfactual_expired_reports_end_pct():
    v = classify_outcome(100.0, [_bar(102, 99, 101.5)],
                         tp_pct=5.0, sl_pct=3.0)
    assert v["outcome"] == "expired"
    assert v["end_pct"] == 1.5


# ── wiring ──────────────────────────────────────────────────────────

def test_registry_and_scanner_wiring():
    reg = open("/app/backend/server_modules/router_registry.py").read()
    assert "routes.missed_entries_admin:router" in reg
    scanner = open("/app/backend/momentum/momentum_scanner.py").read()
    assert "CRYPTO_SCAN_CAP = 30" in scanner  # 2026-08-04 P0 regression lock
    assert "_ignition_additions" in scanner
    life = open("/app/backend/server_modules/lifespan.py").read()
    assert "missed_entries_task" in life
