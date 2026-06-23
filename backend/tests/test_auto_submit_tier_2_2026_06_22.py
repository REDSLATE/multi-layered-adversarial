"""Regression: Tier 2 aggressive preset + tier-picker admin route.

Operator pin (2026-06-22):
    "Keep Tier 1 as the known stable baseline. Add Tier 2 as a
    clearly labeled operator choice. That gives you Conservative =
    stable, Aggressive = deliberate switch — rather than silently
    turning Tier 1 into something it was not designed to be."

Doctrine pins:
  • Tier 2 has confidence_min=0.45 and notional_default_usd=$25.
  • Tier 2 preserves every rail Tier 1 has: same lanes/actions/
    brains/notional_max/dry_run_state.
  • Tier 2 MUST NOT widen `notional_max_usd` — keeps the operator's
    hard ceiling intact.
  • Switching tiers requires a typed audit reason (≥4 chars).
  • The audit row records the transition string explicitly.
"""
from __future__ import annotations

import sys
import pytest

sys.path.insert(0, "/app/backend")


# ── Preset definitions ─────────────────────────────────────────────


def test_tier_2_aggressive_thresholds():
    """The two operator-pinned values on tier 2 are exact: 0.45 and
    $25. Any silent drift here breaks the operator's mental model."""
    from shared.auto_submit_policy import TIER_2_AGGRESSIVE

    assert TIER_2_AGGRESSIVE["tier_name"] == "tier_2_aggressive"
    assert TIER_2_AGGRESSIVE["confidence_min"] == 0.45
    assert TIER_2_AGGRESSIVE["notional_default_usd"] == 25.0


def test_tier_2_preserves_every_rail_of_tier_1():
    """Operator pin: 'same lanes/actions/brains, dry_run must still
    pass.' Tier 2 is NOT a place to loosen RoadGuard or the dry-run
    requirement — only confidence_min and notional_default_usd.
    """
    from shared.auto_submit_policy import (
        TIER_1_DEFAULTS, TIER_2_AGGRESSIVE,
    )

    for rail in (
        "allowed_lanes",
        "allowed_actions",
        "allowed_brains",
        "required_dry_run_state",
        "notional_max_usd",
    ):
        assert TIER_1_DEFAULTS[rail] == TIER_2_AGGRESSIVE[rail], (
            f"Tier 2 must preserve {rail!r} from Tier 1. "
            f"Tier 1={TIER_1_DEFAULTS[rail]!r}, "
            f"Tier 2={TIER_2_AGGRESSIVE[rail]!r}. "
            f"Operator doctrine: Tier 2 only loosens confidence_min "
            f"and notional_default_usd — every other rail stays."
        )


def test_tier_registry_includes_both_tiers():
    from shared.auto_submit_policy import TIER_REGISTRY

    assert "tier_1_conservative" in TIER_REGISTRY
    assert "tier_2_aggressive" in TIER_REGISTRY
    # Future-proofing: more tiers may be added but these two MUST
    # always be present (operator-pinned baseline + aggressive).


def test_get_tier_defaults_rejects_unknown_name():
    """Defensive: an unknown tier_name must raise ValueError so the
    admin route can convert it to HTTP 400 (rather than silently
    falling through to Tier 1)."""
    from shared.auto_submit_policy import get_tier_defaults

    with pytest.raises(ValueError, match="unknown tier_name"):
        get_tier_defaults("tier_99_yolo")


# ── set_policy_async tier handling ────────────────────────────────


@pytest.mark.asyncio
async def test_set_policy_async_applies_tier_preset_atomically(monkeypatch):
    """When `tier_name` is passed, every field of that tier preset
    lands on the in-memory override in ONE write — no partial state."""
    from shared import auto_submit_policy as mod

    async def fake_persist(snap):
        return None
    monkeypatch.setattr(mod, "_persist_to_mongo", fake_persist)
    mod.reset_policy_for_tests()

    policy = await mod.set_policy_async(
        enabled=True, tier_name="tier_2_aggressive",
    )
    assert policy["tier_name"] == "tier_2_aggressive"
    assert policy["confidence_min"] == 0.45
    assert policy["notional_default_usd"] == 25.0
    # Rails preserved
    assert policy["notional_max_usd"] == 5000.0
    assert policy["required_dry_run_state"] == "passed"
    assert policy["enabled"] is True


@pytest.mark.asyncio
async def test_explicit_overrides_take_precedence_over_tier_preset(monkeypatch):
    """Operator may want Tier 2 baseline BUT with confidence_min
    pinned to 0.50 instead of 0.45. The explicit kwarg must win."""
    from shared import auto_submit_policy as mod

    async def fake_persist(snap): return None
    monkeypatch.setattr(mod, "_persist_to_mongo", fake_persist)
    mod.reset_policy_for_tests()

    policy = await mod.set_policy_async(
        enabled=True,
        tier_name="tier_2_aggressive",
        confidence_min=0.50,  # operator pin overrides Tier 2's 0.45
    )
    assert policy["tier_name"] == "tier_2_aggressive"
    assert policy["confidence_min"] == 0.50, (
        "explicit override must win over tier preset; got "
        f"{policy['confidence_min']}"
    )
    # The non-overridden field still comes from the tier
    assert policy["notional_default_usd"] == 25.0


@pytest.mark.asyncio
async def test_round_trip_tier_1_after_tier_2(monkeypatch):
    """Operator switches to Tier 2, then back to Tier 1 — Tier 1's
    full preset must be restored, NOT a hybrid of the two."""
    from shared import auto_submit_policy as mod

    async def fake_persist(snap): return None
    monkeypatch.setattr(mod, "_persist_to_mongo", fake_persist)
    mod.reset_policy_for_tests()

    await mod.set_policy_async(enabled=True, tier_name="tier_2_aggressive")
    p2 = mod.get_policy()
    assert p2["confidence_min"] == 0.45
    assert p2["notional_default_usd"] == 25.0

    await mod.set_policy_async(enabled=True, tier_name="tier_1_conservative")
    p1 = mod.get_policy()
    assert p1["tier_name"] == "tier_1_conservative"
    assert p1["confidence_min"] == 0.70   # Tier 1 baked-in default
    assert p1["notional_default_usd"] == 10.0


@pytest.mark.asyncio
async def test_set_policy_async_no_tier_name_keeps_current_overrides(monkeypatch):
    """When `tier_name` is None, the call behaves exactly like the
    pre-2026-06-22 contract — explicit overrides only, no tier swap.
    This preserves the existing 'tweak confidence_min in place'
    workflow."""
    from shared import auto_submit_policy as mod

    async def fake_persist(snap): return None
    monkeypatch.setattr(mod, "_persist_to_mongo", fake_persist)
    mod.reset_policy_for_tests()

    # Start at Tier 1, then bump confidence_min without naming a tier
    policy = await mod.set_policy_async(
        enabled=True, confidence_min=0.55,
    )
    assert policy["tier_name"] == "tier_1_conservative"
    assert policy["confidence_min"] == 0.55
    assert policy["notional_default_usd"] == 10.0  # untouched
