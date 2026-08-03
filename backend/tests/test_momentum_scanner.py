"""Momentum scanner integration tests (2026-08-01)."""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from momentum.momentum_scanner import (  # noqa: E402
    DEFAULTS, build_snapshot, momentum_score, relative_volume,
)
from momentum.momentum_position_controller import (  # noqa: E402
    EntryPolicy, valid_momentum_entry,
)

pytestmark = pytest.mark.tripwire


def _bars(closes, vols=None, base_ts="2026-08-01T15:{m:02d}:00+00:00"):
    vols = vols or [100_000.0] * len(closes)
    out = []
    for i, (c, v) in enumerate(zip(closes, vols)):
        o = closes[i - 1] if i else c
        out.append({"ts": base_ts.format(m=i), "o": o, "c": c,
                    "h": max(o, c) * 1.001, "l": min(o, c) * 0.999, "v": v})
    return out


def test_score_flat_tape_below_threshold():
    closes = [10.0] * 14
    assert momentum_score(closes, [1000.0] * 14) == 0.5


def test_score_rising_tape_accelerates():
    # fresh transition: flat tape, then two accelerating green bars
    closes = [10.0] * 11 + [10.02, 10.08, 10.20]
    vols = [50_000.0] * 12 + [80_000.0, 120_000.0]
    cur = momentum_score(closes, vols)
    prev = momentum_score(closes[:-1], vols[:-1])
    assert cur is not None and prev is not None
    assert cur > prev and (cur - prev) >= 0.08
    assert cur >= 0.60 > prev


def test_confirmation_price_is_transition_bar():
    from momentum.momentum_scanner import confirmation_price
    closes = [10.0] * 11 + [10.02, 10.08, 10.20]
    vols = [50_000.0] * 12 + [80_000.0, 120_000.0]
    # score crosses 0.60 on the final bar → confirmation = its close
    assert confirmation_price(closes, vols) == 10.20


def test_relative_volume():
    assert relative_volume([100.0] * 12 + [250.0]) == 2.5
    assert relative_volume([100.0] * 5) == 1.0  # thin tape → neutral


def test_build_snapshot_and_entry_allowed():
    # fresh transition bar → confirmation is current close → no chase
    closes = [10.0] * 11 + [10.02, 10.08, 10.20]
    vols = [50_000.0] * 12 + [80_000.0, 120_000.0]
    bars = _bars(closes, vols)
    snap = build_snapshot("SOL/USD", bars, bid=10.20, ask=10.21,
                          quote_age_ms=200)
    assert snap is not None
    assert snap.price_above_ema9 and snap.price_above_vwap
    d = valid_momentum_entry(snap, EntryPolicy())
    assert d.allowed, d.reason
    assert d.intent_payload["requires_standard_entry_gates"] is True


def test_build_snapshot_stale_momentum_rejected():
    # momentum confirmed several bars ago and price kept running —
    # the run since the transition bar must reject (chase territory)
    closes = [10.0] * 8 + [10.05, 10.15, 10.28, 10.42, 10.55, 10.70]
    vols = [50_000.0] * 8 + [120_000.0] * 6
    snap = build_snapshot("SOL/USD", _bars(closes, vols),
                          bid=10.70, ask=10.71, quote_age_ms=200)
    d = valid_momentum_entry(snap, EntryPolicy())
    assert not d.allowed
    assert d.reason in ("maximum_chase_exceeded",
                        "momentum_not_accelerating")
    assert float(snap.confirmation_price) < 10.70


def test_build_snapshot_thin_tape_none():
    assert build_snapshot("X/USD", _bars([10.0] * 5),
                          bid=10, ask=10.01, quote_age_ms=0) is None


def test_defaults_ship_disarmed():
    assert DEFAULTS["enabled"] is False
    assert DEFAULTS["tp_pct"] == 5.0 and DEFAULTS["sl_pct"] == 3.0
    assert DEFAULTS["lanes"] == ["crypto", "equity"]


def test_equity_spread_cap_tighter_than_crypto():
    # 100 bps spread: fine for crypto (250 cap), rejected for equity (75)
    closes = [10.0] * 11 + [10.02, 10.08, 10.20]
    vols = [50_000.0] * 12 + [80_000.0, 120_000.0]
    bars = _bars(closes, vols)
    eq = build_snapshot("NVDA", bars, bid=10.15, ask=10.25,
                        quote_age_ms=200, lane="equity")
    cr = build_snapshot("SOL/USD", bars, bid=10.15, ask=10.25,
                        quote_age_ms=200, lane="crypto")
    assert not valid_momentum_entry(eq, EntryPolicy()).allowed
    assert valid_momentum_entry(
        eq, EntryPolicy()).reason == "spread_too_wide"
    assert valid_momentum_entry(cr, EntryPolicy()).reason != "spread_too_wide"


def test_equity_lane_wiring_present():
    src = open("/app/backend/momentum/momentum_scanner.py").read()
    assert "is_equity_rth" in src
    assert "equity_market_closed" in src
    assert "read_all_universes" in src
    assert 'no_quote' in src  # equity fails closed on missing quotes


def test_momentum_is_a_valid_intent_stack():
    from shared.intents import IntentIn
    body = IntentIn(stack="momentum", action="BUY", symbol="BTC/USD",
                    lane="crypto", confidence=0.7, rationale="test")
    assert body.stack == "momentum"


def test_wiring_present():
    reg = open("/app/backend/server_modules/router_registry.py").read()
    assert "routes.momentum_scanner_routes:router" in reg
    life = open("/app/backend/server_modules/lifespan.py").read()
    assert "momentum_scanner_task" in life
    mon = open("/app/backend/shared/exits/monitor.py").read()
    assert "momentum_policy" in mon and "get_momentum_exit_pcts" in mon


def test_red_bar_volume_spike_never_scores_as_momentum():
    """ICNT 2026-08-03 replay finding: a falling bar with 15x volume
    (distribution) must not cross the momentum threshold."""
    closes = [10.0] * 12 + [10.02, 9.90]      # last bar red -1.2%
    vols = [10_000.0] * 13 + [150_000.0]      # 15x spike on the red bar
    s = momentum_score(closes, vols)
    assert s is not None and s < 0.50


def test_volume_kicker_capped_on_green_bars():
    # same 15x spike on a modest green bar: kicker capped at 4x,
    # cannot carry the score past threshold alone
    closes = [10.0] * 12 + [10.0, 10.03]
    vols = [10_000.0] * 13 + [150_000.0]
    s = momentum_score(closes, vols)
    assert s is not None and s < 0.60
