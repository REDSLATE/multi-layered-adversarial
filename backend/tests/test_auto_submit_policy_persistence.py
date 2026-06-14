"""Regression test for the 2026-02-19 prod incident.

Operator on production: "I flipped the auto-submit toggle and nothing
happened." Investigation showed the policy override lived in a
module-level dict with ZERO Mongo persistence. Every K8s pod restart
silently reset the toggle to default-off.

This test pins the fix: a flip via set_policy_async survives a
simulated process restart (we clear the in-memory cache, call
hydrate_from_mongo, and confirm the override is restored from Mongo).
"""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


async def _reset_state():
    """Wipe both the in-memory cache AND the Mongo singleton so each
    test starts from a known floor."""
    from db import db
    from shared.auto_submit_policy import (
        POLICY_STATE_COLL,
        POLICY_STATE_DOC_ID,
        reset_policy_for_tests,
    )
    reset_policy_for_tests()
    await db[POLICY_STATE_COLL].delete_one({"_id": POLICY_STATE_DOC_ID})


async def test_set_policy_async_persists_to_mongo():
    """Calling set_policy_async writes the override to the
    `shared_auto_submit_policy_state` singleton."""
    from db import db
    from shared.auto_submit_policy import (
        POLICY_STATE_COLL,
        POLICY_STATE_DOC_ID,
        set_policy_async,
    )
    await _reset_state()
    await set_policy_async(enabled=True, confidence_min=0.9)
    doc = await db[POLICY_STATE_COLL].find_one({"_id": POLICY_STATE_DOC_ID})
    assert doc is not None, "set_policy_async must write to Mongo"
    assert doc["enabled"] is True
    assert doc["confidence_min"] == 0.9
    assert "updated_at" in doc


async def test_toggle_survives_simulated_pod_restart():
    """The actual incident scenario: operator flips ON, pod restarts,
    policy must still be ON after hydrate_from_mongo() runs.
    """
    from shared.auto_submit_policy import (
        get_policy,
        hydrate_from_mongo,
        reset_policy_for_tests,
        set_policy_async,
    )
    await _reset_state()

    # Operator flips ON.
    await set_policy_async(enabled=True)
    assert get_policy()["enabled"] is True
    assert get_policy()["source"] == "runtime_override"

    # Simulate K8s pod restart: process memory wiped.
    reset_policy_for_tests()
    assert get_policy()["enabled"] is False, "memory should be wiped"
    assert get_policy()["source"] == "default_off"

    # Lifespan hook fires hydrate_from_mongo at boot.
    p = await hydrate_from_mongo()
    assert p["enabled"] is True, (
        "after hydrate, the operator's ON state must be restored — "
        "this is the actual fix for the 2026-02-19 prod incident"
    )
    assert get_policy()["enabled"] is True
    assert get_policy()["source"] == "runtime_override"


async def test_toggle_off_also_persists():
    """Disabling the policy must also persist, otherwise a pod restart
    would silently re-enable an explicitly-disabled policy."""
    from shared.auto_submit_policy import (
        get_policy,
        hydrate_from_mongo,
        reset_policy_for_tests,
        set_policy_async,
    )
    await _reset_state()
    await set_policy_async(enabled=True)
    await set_policy_async(enabled=False)
    reset_policy_for_tests()
    await hydrate_from_mongo()
    assert get_policy()["enabled"] is False


async def test_hydrate_handles_empty_mongo():
    """A fresh deployment with no persisted policy must hydrate cleanly
    to default-off (not crash)."""
    from shared.auto_submit_policy import (
        get_policy,
        hydrate_from_mongo,
        reset_policy_for_tests,
    )
    await _reset_state()
    reset_policy_for_tests()
    p = await hydrate_from_mongo()
    assert p["enabled"] is False
    assert p["source"] == "default_off"
    assert get_policy()["enabled"] is False


async def test_broadened_defaults_include_both_lanes_and_actions():
    """Operator directive 2026-02-19: 'I'm not reviewing, it should be
    handled by Shelly and filed.' Defaults must include both lanes
    and both directions so Shelly catches every brain intent."""
    from shared.auto_submit_policy import TIER_1_DEFAULTS
    assert "equity" in TIER_1_DEFAULTS["allowed_lanes"]
    assert "crypto" in TIER_1_DEFAULTS["allowed_lanes"]
    assert "BUY" in TIER_1_DEFAULTS["allowed_actions"]
    assert "SELL" in TIER_1_DEFAULTS["allowed_actions"]
