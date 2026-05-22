"""Tripwire: live-trade-diagnose probe must not block on its OWN missing data.

Doctrine pin (2026-02-18):
    The synthetic probe at `/api/admin/execution/diagnose` is the
    operator's "would a healthy intent route right now?" check. It
    used to construct a sim intent with `snapshot=None`, which fail-
    closed at gate 7 (`roadguard_spread_floor — MISSING_SPREAD_BPS`)
    independent of MC's actual health. The "LIVE TRADE: BLOCKED"
    banner thus stuck on forever, regardless of whether brokers were
    connected, toggles were ON, or seats were held.

    After the 2026-02-18 fix, the synthetic carries a sample
    spread_bps so gate 7 reflects MC's TRUE state. If gate 7 fails
    now, it really means RoadGuard has an issue (impossible — fixed
    sample), so any future "gate 7 BLOCK" on this probe is a regression.
"""
from __future__ import annotations

import pytest


@pytest.mark.tripwire
async def test_probe_carries_sample_snapshot():
    """The synthetic intent the probe builds MUST include `snapshot`
    with a non-None `spread_bps`. Otherwise it pre-blocks itself."""
    from shared.execution import execution_diagnose as live_trade_diagnose
    for lane in ("crypto", "equity"):
        result = await live_trade_diagnose(lane=lane, notional_usd=100.0, _user={})
        sim = result["synthetic_intent"]
        snap = sim.get("snapshot") or {}
        assert snap.get("spread_bps") is not None, (
            f"probe for lane={lane} MUST carry sample spread_bps; "
            f"otherwise gate 7 false-blocks the whole diagnostic. "
            f"got snapshot={snap!r}"
        )


@pytest.mark.tripwire
async def test_probe_gate_7_passes_with_sample_snapshot():
    """Gate 7 (`roadguard_spread_floor`) MUST PASS on the probe.
    The sample is set well below the spread cap (5 bps equity / 12 bps
    crypto vs 50 / 200 caps). Failure here = doctrine drift."""
    from shared.execution import execution_diagnose as live_trade_diagnose
    for lane in ("crypto", "equity"):
        result = await live_trade_diagnose(lane=lane, notional_usd=100.0, _user={})
        rg = next(
            (g for g in result["gates"] if g["name"] == "roadguard_spread_floor"),
            None,
        )
        assert rg is not None
        assert rg["passed"] is True, (
            f"probe gate 7 failed for lane={lane}: {rg!r} — the probe "
            f"is supposed to be a clean baseline; if it can't pass "
            f"RoadGuard on a fixed sample, MC's gate logic has drifted"
        )


@pytest.mark.tripwire
async def test_probe_first_blocker_no_longer_misleadingly_says_snapshot_absent():
    """A genuine MC-side block (no broker creds, lane toggle off,
    governor seat vacant) is a real signal; a self-induced gate-7 block
    is a false alarm. After the fix, no probe verdict should ever cite
    `ROADGUARD_MISSING_SPREAD_BPS` because the sample snapshot fixes
    that input."""
    from shared.execution import execution_diagnose as live_trade_diagnose
    for lane in ("crypto", "equity"):
        result = await live_trade_diagnose(lane=lane, notional_usd=100.0, _user={})
        fb = result.get("first_blocker")
        if fb:
            reason = fb.get("reason") or ""
            assert "MISSING_SPREAD_BPS" not in reason, (
                f"probe first_blocker for lane={lane} still cites "
                f"ROADGUARD_MISSING_SPREAD_BPS — the synthetic should "
                f"never trip this. got: {fb!r}"
            )
