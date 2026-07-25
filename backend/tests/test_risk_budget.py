"""Daily budget on the LIVE risk gate — snapshot + local counter
(2026-07-24 hot-path audit rewrite of the 2026-07-22 tests).

The gate no longer aggregates Atlas `executions` per intent: spend is
a local SQLite-persisted counter and the cap override lives in the
ExecutionPolicySnapshot.
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "/app/backend")

from shared.hotpath import daily_spend, intent_queue, outbox, policy_snapshot
from shared.risk.check import _daily_cap_effective, _daily_spent_usd, check


@pytest.fixture
def hp(tmp_path):
    outbox.reset_for_tests(str(tmp_path / "hp.sqlite"))
    policy_snapshot.reset_for_tests()
    daily_spend.reset_for_tests()
    intent_queue.reset_for_tests()
    yield
    import os
    outbox.reset_for_tests(os.environ.get("HOTPATH_DB_PATH", "/app/backend/data/hotpath.sqlite"))
    policy_snapshot.reset_for_tests()
    daily_spend.reset_for_tests()
    intent_queue.reset_for_tests()


def _arm_snapshot(**overrides):
    fields = dict(
        master_switch_enabled=True,
        lane_enabled={"equity": True, "crypto": True},
        broker_frozen=False,
        cap_daily_usd_override=None,
    )
    fields.update(overrides)
    policy_snapshot.mark_dirty()          # would refresh — we pin instead
    policy_snapshot._dirty = False        # noqa: SLF001 — deterministic unit state
    policy_snapshot.apply_local(**fields)


@pytest.mark.asyncio
async def test_cap_override_beats_env_and_reverts(hp, monkeypatch):
    monkeypatch.setenv("RISEDUAL_CAP_DAILY_USD", "1000")
    _arm_snapshot()
    assert await _daily_cap_effective() == 1000.0
    _arm_snapshot(cap_daily_usd_override=2500.0)
    assert await _daily_cap_effective() == 2500.0
    _arm_snapshot(cap_daily_usd_override=None)
    assert await _daily_cap_effective() == 1000.0


@pytest.mark.asyncio
async def test_spend_counter_add_and_reset(hp):
    _arm_snapshot()
    assert await _daily_spent_usd() == 0.0
    daily_spend.add(400.0)
    assert await _daily_spent_usd() == 400.0
    daily_spend.reset()
    assert await _daily_spent_usd() == 0.0


@pytest.mark.asyncio
async def test_risk_check_unblocks_after_reset(hp, monkeypatch):
    """The exact prod scenario: cap exhausted → RISK reject; RESET
    SPEND → same intent passes."""
    monkeypatch.setenv("RISEDUAL_CAP_DAILY_USD", "50")
    _arm_snapshot()
    intent = {"intent_id": "budget-test-1", "lane": "crypto"}
    daily_spend.add(49.0)
    r = await check(intent, notional_usd=5.0)
    assert r.ok is False
    assert r.reason.startswith("daily_cap_exceeded")

    daily_spend.reset()
    r2 = await check(intent, notional_usd=5.0)
    assert r2.ok is True, f"expected pass after reset, got {r2.reason}"


@pytest.mark.asyncio
async def test_spend_counter_survives_restart(hp):
    """SQLite persistence: memory wipe (simulated restart) recovers
    today's spend."""
    _arm_snapshot()
    daily_spend.add(123.45)
    daily_spend._mem.update(day=None, spent=0.0, reset_at=None)  # noqa: SLF001
    assert await _daily_spent_usd() == pytest.approx(123.45)


@pytest.mark.asyncio
async def test_atlas_reset_marker_reconciles_counter(hp):
    """A NEWER reset marker observed by the snapshot refresher zeroes
    the local counter; the same marker seen again is a no-op."""
    from datetime import datetime, timezone
    _arm_snapshot()
    daily_spend.add(75.0)
    marker = datetime.now(timezone.utc).isoformat()
    daily_spend.observe_reset_marker(marker)
    assert daily_spend.get_spent() == 0.0
    daily_spend.add(10.0)
    daily_spend.observe_reset_marker(marker)  # same marker → no reset
    assert daily_spend.get_spent() == 10.0
