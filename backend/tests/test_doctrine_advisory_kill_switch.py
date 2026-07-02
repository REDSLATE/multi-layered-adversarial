"""Tests for the DOCTRINE_ADVISORY_ENABLED kill switch (2026-07-03).

Locks the operator's pin: the doctrine advisory layer is optional and
can be flipped off from env. When off, `_build_and_persist_doctrine_packet`:
    * returns a well-shaped stub (not None, not raising)
    * does NOT import lane_doctrine_router
    * does NOT write to `doctrine_sidecars`
    * does NOT emit a Shelly event

Test double strategy: patch the module-level `db` and `record_async`
so the test can observe whether they were touched. When they ARE
touched with flag off, the test fails.
"""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/backend")


@pytest.mark.asyncio
async def test_flag_off_returns_stub_without_touching_router(monkeypatch):
    """Advisory disabled → stub returned; router import never runs."""
    from shared import intents

    monkeypatch.setenv("DOCTRINE_ADVISORY_ENABLED", "false")

    # If the router IS imported the test fails loudly. We patch
    # sys.modules so any accidental import raises.
    forbidden_router = "shared.doctrine.lane_doctrine_router"

    class _Explode:
        def __getattr__(self, name):
            raise AssertionError(
                f"lane_doctrine_router accessed while flag OFF (attr={name})"
            )
    monkeypatch.setitem(sys.modules, forbidden_router, _Explode())

    result = await intents._build_and_persist_doctrine_packet(
        intent_id="i-test-001",
        stack="camino",
        lane="equity",
        symbol="AMD",
        action="BUY",
        confidence=0.7,
        snapshot={"foo": "bar"},
        ingest_method="test",
    )
    assert result is not None
    assert result["advisory_disabled"] is True
    assert result["doctrine_version"] == "advisory_disabled_v1"
    assert result["symbol"] == "AMD"
    assert result["lane"] == "equity"
    # Shape parity with the failure envelope — callers that already
    # tolerate `_doctrine_failure_packet` also tolerate this.
    assert "base_labels" in result and "seats" in result


@pytest.mark.asyncio
async def test_flag_off_skips_mongo_and_shelly_writes(monkeypatch):
    """Advisory disabled → no `doctrine_sidecars` insert, no Shelly event."""
    from shared import intents

    monkeypatch.setenv("DOCTRINE_ADVISORY_ENABLED", "false")

    fake_collection = MagicMock()
    fake_collection.insert_one = AsyncMock()
    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(return_value=fake_collection)
    monkeypatch.setattr(intents, "db", fake_db)

    fake_record = MagicMock()
    with patch("shared.mc_shelly.record_async", fake_record):
        result = await intents._build_and_persist_doctrine_packet(
            intent_id="i-test-002",
            stack="barracuda",
            lane="crypto",
            symbol="BTCUSD",
            action="SELL",
            confidence=0.6,
            snapshot={},
            ingest_method="test",
        )

    assert result["advisory_disabled"] is True
    # Mongo audit row must NOT be written.
    assert fake_collection.insert_one.await_count == 0
    # Shelly event must NOT be emitted.
    assert fake_record.call_count == 0


@pytest.mark.asyncio
async def test_flag_on_preserves_current_ingest_shape(monkeypatch):
    """Default-on case: the packet retains the router-built shape.

    We don't run the real router (needs full snapshot fields); we
    stub it to a minimal valid packet and confirm the caller flows
    through to the write path (not the disabled stub path).
    """
    from shared import intents

    monkeypatch.setenv("DOCTRINE_ADVISORY_ENABLED", "true")

    stub_packet = {
        "doctrine_version": "gap_and_go_v1",
        "seats": {},
        "base_labels": {
            "score": 0.42, "quality": "C_QUALITY",
            "labels": [], "reasons": [],
        },
    }

    fake_router = MagicMock()
    fake_router.build_lane_doctrine_packet = MagicMock(return_value=stub_packet)
    fake_router.fetch_seat_holders = AsyncMock(return_value={})
    fake_router.hoist_packet_audit_fields = MagicMock(return_value={
        "quality": "C_QUALITY", "score": 0.42, "doctrine_version": "gap_and_go_v1",
        "strategist_conviction_delta": 0.0, "strategist_holder": None,
        "adversary_challenge_required": False, "adversary_challenge_strength": 0.0,
        "adversary_objection_count": 0, "adversary_holder": None,
        "governor_action": "modulate", "governor_risk_multiplier": 1.0,
        "governor_block_reason_count": 0, "governor_holder": None,
        "execution_judge_ready": True, "execution_judge_holder": None,
        "execution_judge_failed_checks": [],
        "execution_judge_not_ready_reason": None,
        "redeye_challenge_required": False,
        "chevelle_governor_action": "modulate",
        "camaro_execution_ready": True,
    })
    monkeypatch.setitem(
        sys.modules, "shared.doctrine.lane_doctrine_router", fake_router,
    )

    fake_collection = MagicMock()
    fake_collection.insert_one = AsyncMock()
    fake_db = MagicMock()
    fake_db.__getitem__ = MagicMock(return_value=fake_collection)
    monkeypatch.setattr(intents, "db", fake_db)

    result = await intents._build_and_persist_doctrine_packet(
        intent_id="i-test-003",
        stack="camino",
        lane="equity",
        symbol="AMD",
        action="BUY",
        confidence=0.7,
        snapshot={},
        ingest_method="test",
    )
    # Flag ON: full packet, no stub marker.
    assert "advisory_disabled" not in result
    assert result["doctrine_version"] == "gap_and_go_v1"
    # Mongo write path WAS exercised.
    assert fake_collection.insert_one.await_count == 1


def test_advisory_enabled_flag_parses_common_values(monkeypatch):
    """Flag helper is tolerant of common truthy/falsy strings."""
    from shared import intents

    for truthy in ("true", "1", "yes", "on", "TRUE", "  True  "):
        monkeypatch.setenv("DOCTRINE_ADVISORY_ENABLED", truthy)
        assert intents._doctrine_advisory_enabled() is True, truthy
    for falsy in ("false", "0", "no", "off", "", "FALSE"):
        monkeypatch.setenv("DOCTRINE_ADVISORY_ENABLED", falsy)
        assert intents._doctrine_advisory_enabled() is False, falsy


def test_default_is_enabled_when_env_unset(monkeypatch):
    """Backward-compat: preview + existing prod behavior preserved by default."""
    from shared import intents
    monkeypatch.delenv("DOCTRINE_ADVISORY_ENABLED", raising=False)
    assert intents._doctrine_advisory_enabled() is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
