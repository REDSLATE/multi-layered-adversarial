"""Phase 4 ENGAGED — sizing gate reads ladder stage and clamps + routes.

Doctrine pin (2026-02-17): until Phase 4 shipped, the brain's
self-zero (`size_multiplier=0` / `would_trade_without_gates=false`)
asymmetrically forced the shadow path. Alpha (never self-zeroed)
fired through; Camaro/Chevelle/REDEYE (self-zeroed) got permanently
shadowed regardless of the operator's ladder promotions.

Phase 4 routes by LADDER STAGE, not by brain self-declaration:

    observation_only → route=observe,   final_usd=0,  execution_mode=None
    micro_paper      → route=paper,     final_usd=$LADDER_MICRO_PAPER_USD,
                                         execution_mode="ladder_paper"
    micro_live       → route=live_micro, final_usd=$LADDER_MICRO_LIVE_USD,
                                         execution_mode="ladder_live_micro"
    normal_live      → route=live_normal, lane-cap binding,
                                         execution_mode="live"
"""
from __future__ import annotations

import pytest

from shared.learning_ladder import _set_stage
from shared.sizing_gate import (
    LADDER_MICRO_LIVE_USD,
    LADDER_MICRO_PAPER_USD,
    ROUTE_LIVE_MICRO,
    ROUTE_LIVE_NORMAL,
    ROUTE_OBSERVE,
    ROUTE_PAPER,
    evaluate_sizing,
    evaluate_sizing_with_ladder,
)


BRAIN = "redeye"  # the brain that historically only emitted self-zeroed
LANE = "equity"


@pytest.fixture(autouse=True)
async def _reset_stage():
    """Always start/end each test at observation_only so the suite is
    order-independent."""
    await _set_stage(BRAIN, LANE, "observation_only", "test", "phase4_test_setup")
    yield
    await _set_stage(BRAIN, LANE, "observation_only", "test", "phase4_test_teardown")


async def test_observation_only_forces_observe_route_and_zero_size():
    s = await evaluate_sizing_with_ladder(50.0, BRAIN, LANE)
    assert s.stage == "observation_only"
    assert s.route == ROUTE_OBSERVE
    assert s.final_usd == 0.0
    assert s.was_clamped is True
    assert s.binding_rail == "ladder_observation"
    # No execution mode at observation_only — there's no broker fill.
    assert s.execution_mode is None


async def test_micro_paper_clamps_to_ladder_paper_cap_and_tags_execution_mode():
    """Verifies: at stage=micro_paper, the sizing gate clamps to the
    SMALLER of the ladder-paper cap, the micro_live cap (if enabled),
    and the lane cap. Tags receipt with `ladder_paper`. The exact
    value depends on operator config — pilot has micro_live=$5 <
    ladder_paper=$10, so micro_live binds. Pre-pilot, ladder_paper
    bound. Either way the invariant is `was_clamped=True` and
    `final == min(...all rails)`."""
    from shared.exposure_caps import cap_for_lane
    from shared.sizing_gate import MICRO_LIVE_ENABLED, _micro_live_cap_for
    await _set_stage(BRAIN, LANE, "micro_paper", "test", "phase4_paper")
    s = await evaluate_sizing_with_ladder(50.0, BRAIN, LANE)
    expected_rails = [LADDER_MICRO_PAPER_USD, cap_for_lane(LANE)]
    if MICRO_LIVE_ENABLED:
        expected_rails.append(_micro_live_cap_for(LANE))
    assert s.stage == "micro_paper"
    assert s.route == ROUTE_PAPER
    assert s.final_usd == min(expected_rails)
    assert s.was_clamped is True
    assert s.execution_mode == "ladder_paper"


async def test_micro_live_clamps_to_ladder_live_cap_and_tags_execution_mode():
    """Same smallest-wins invariant — at micro_live, the gate clamps
    to min(ladder_micro_live, micro_live_cap, lane_cap)."""
    from shared.exposure_caps import cap_for_lane
    from shared.sizing_gate import MICRO_LIVE_ENABLED, _micro_live_cap_for
    await _set_stage(BRAIN, LANE, "micro_paper", "test", "phase4_step")
    await _set_stage(BRAIN, LANE, "micro_live", "test", "phase4_live_micro")
    s = await evaluate_sizing_with_ladder(50.0, BRAIN, LANE)
    expected_rails = [LADDER_MICRO_LIVE_USD, cap_for_lane(LANE)]
    if MICRO_LIVE_ENABLED:
        expected_rails.append(_micro_live_cap_for(LANE))
    assert s.stage == "micro_live"
    assert s.route == ROUTE_LIVE_MICRO
    assert s.final_usd == min(expected_rails)
    assert s.execution_mode == "ladder_live_micro"


async def test_normal_live_uses_lane_cap_only_no_ladder_clamp():
    await _set_stage(BRAIN, LANE, "micro_paper", "test", "step")
    await _set_stage(BRAIN, LANE, "micro_live", "test", "step")
    await _set_stage(BRAIN, LANE, "normal_live", "test", "phase4_normal")
    s = await evaluate_sizing_with_ladder(50.0, BRAIN, LANE)
    assert s.stage == "normal_live"
    assert s.route == ROUTE_LIVE_NORMAL
    # At normal_live the ladder rail drops out; min(lane_cap, micro_live_cap).
    from shared.exposure_caps import cap_for_lane
    from shared.sizing_gate import MICRO_LIVE_ENABLED, _micro_live_cap_for
    rails = [50.0, cap_for_lane(LANE)]
    if MICRO_LIVE_ENABLED:
        rails.append(_micro_live_cap_for(LANE))
    assert s.final_usd == min(rails)
    assert s.ladder_cap_usd is None  # ladder is OFF at top rung
    assert s.execution_mode == "live"


async def test_legacy_evaluate_sizing_still_returns_no_ladder_fields():
    """Backward-compat: legacy entry point (manual /execution/submit)
    must still return a usable SizingDecision without ladder fields.
    Final value = min(requested, lane_cap, micro_live_cap_if_enabled)."""
    s = evaluate_sizing(50.0, "equity")
    from shared.exposure_caps import cap_for_lane
    from shared.sizing_gate import MICRO_LIVE_ENABLED, _micro_live_cap_for
    rails = [50.0, cap_for_lane("equity")]
    if MICRO_LIVE_ENABLED:
        rails.append(_micro_live_cap_for("equity"))
    assert s.final_usd == min(rails)
    assert s.stage is None
    assert s.route is None
    assert s.ladder_cap_usd is None
    assert s.execution_mode is None
