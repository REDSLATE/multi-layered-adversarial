"""Entry Timing Gate replay tests (2026-08-01 operator doctrine).

Covers the operator's required scenarios with synthetic bar tapes:
early entries allowed, extended/parabolic/late chases blocked,
JDZG-shaped price action ($8.42 confirmation → $12.47 chase) blocked,
exits never gated, per-class profiles differ, fail-closed on missing
data, and the gate ships ENABLED.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.risk_sizer.entry_timing import (  # noqa: E402
    DEFAULT_PROFILES, evaluate,
)

pytestmark = pytest.mark.tripwire


def _bar(o, c, h=None, l=None, v=100_000, ts="2026-08-01T14:{m:02d}:00+00:00", m=0):
    return {"o": o, "c": c, "h": h if h is not None else max(o, c) * 1.002,
            "l": l if l is not None else min(o, c) * 0.998, "v": v,
            "ts": ts.format(m=m)}


def _tape(prices, vols=None):
    bars = []
    for i, p in enumerate(prices):
        prev = prices[i - 1] if i else p
        v = vols[i] if vols else 100_000
        bars.append(_bar(prev, p, v=v, m=i))
    return bars


def _intent(price, symbol="JDZG", lane="equity", **kw):
    return {"intent_id": "t1", "symbol": symbol, "lane": lane,
            "action": "BUY", "snapshot": {"price": price,
                                          "market_cap_band": "small"},
            "ingest_ts": "2026-08-01T14:00:00+00:00", **kw}


def test_early_entry_near_confirmation_allowed():
    # flat tape, price barely above confirmation, near VWAP
    bars = _tape([8.40] * 20 + [8.42, 8.45])
    v = evaluate(_intent(8.42), bars, DEFAULT_PROFILES)
    assert v["allowed"], v
    assert v["receipt"]["extension_from_confirmation_pct"] < 1.0


def test_jdzg_shape_chase_is_blocked():
    # confirmed at $8.42, price ran to $12.47 → 48% extension
    bars = _tape([8.4, 8.6, 9.1, 9.8, 10.5, 11.2, 11.9, 12.3, 12.4,
                  12.45, 12.47, 12.47])
    v = evaluate(_intent(8.42), bars, DEFAULT_PROFILES)
    assert not v["allowed"]
    assert v["reason"] == "MISSED_ENTRY_CHASE_RISK"
    assert v["decision"] == "MISSED_ENTRY"
    assert "8.42" in v["receipt"]["message"]
    assert "12.47" in v["receipt"]["message"]


def test_parabolic_phase_blocked_for_small_cap():
    # violent accelerating tape w/ exploding volume → parabolic phase
    prices = [10.0, 10.05, 10.1, 10.1, 10.15, 10.2, 10.2, 10.25,
              10.3, 10.3, 10.4, 10.6, 11.0, 11.6, 12.4]
    vols = [100_000] * 11 + [400_000, 900_000, 1_500_000, 2_500_000]
    bars = _tape(prices, vols)
    v = evaluate(_intent(12.3), bars, DEFAULT_PROFILES)
    assert not v["allowed"]
    assert v["reason"] in ("PARABOLIC_CHASE_RISK", "TOO_FAR_ABOVE_VWAP",
                           "MOVE_ALREADY_EXTENDED")


def test_fade_after_peak_is_late_momentum():
    # ran to 15, now fading at 12.9 (>10% off peak) — intent conf 12.8
    prices = [10, 11, 12, 13, 14, 15, 14.6, 14.0, 13.5, 13.2, 13.0, 12.9]
    bars = _tape(prices)
    v = evaluate(_intent(12.8), bars, DEFAULT_PROFILES)
    assert not v["allowed"]
    assert v["reason"] == "LATE_MOMENTUM_ENTRY"
    assert v["receipt"]["parabolic_phase"] == "fade"


def test_large_cap_uses_tighter_profile():
    # +3% above confirmation: fine for small-cap (8% cap), blocked
    # for large-cap (2% cap)
    bars = _tape([100.0] * 18 + [102.0, 103.0])
    small = evaluate(_intent(100.0), bars, DEFAULT_PROFILES)
    large_intent = _intent(100.0, symbol="NVDA")
    large_intent["snapshot"] = {"price": 100.0, "market_cap_band": "large"}
    large = evaluate(large_intent, bars, DEFAULT_PROFILES)
    assert small["receipt"]["profile"] == "small_cap_momentum"
    assert large["receipt"]["profile"] == "large_cap"
    assert not large["allowed"]
    assert large["reason"] == "MISSED_ENTRY_CHASE_RISK"


def test_no_bars_fails_closed():
    v = evaluate(_intent(8.42), [], DEFAULT_PROFILES)
    assert not v["allowed"]
    assert v["reason"] == "NO_TIMING_DATA"


def test_gate_ships_enabled_and_wired():
    src = open("/app/backend/shared/auto_router.py").read()
    assert "_gate_entry_timing" in src, "gate stage not wired into router"
    stages = open("/app/backend/shared/auto_router_stages.py").read()
    assert 'if ctx.action_upper != "BUY":' in stages, (
        "exits must NEVER be gated — BUY-only guard missing"
    )
    import re
    m = re.search(r"for stage in \((.*?)\):", src, re.S)
    order = m.group(1)
    assert order.index("_gate_risk") < order.index("_gate_entry_timing") \
        < order.index("_route_and_submit"), "gate must sit between risk and submit"


@pytest.mark.asyncio
async def test_disabled_config_allows(monkeypatch):
    from shared.risk_sizer import entry_timing as mod

    async def _off():
        return {"enabled": False, "profiles": DEFAULT_PROFILES}
    monkeypatch.setattr(mod, "get_config", _off)
    v = await mod.check_buy_entry(_intent(8.42))
    assert v["allowed"] and v["reason"] == "gate_disabled"
