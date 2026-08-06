"""Exit policy last-known-good fallback (2026-07-23 hot-path audit
P0 fix): an Atlas outage must NOT silently disable stop-loss
enforcement for open positions."""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, "/app/backend")

from shared.exits import policy as ep


@pytest.mark.asyncio
async def test_atlas_outage_returns_last_known_good_not_disabled():
    stored = {"crypto": {"enabled": True, "sl_pct": 2.5}}
    fake_flags = AsyncMock()
    fake_flags.find_one = AsyncMock(return_value={"_id": "exit_policy", **stored})

    class _DB(dict):
        def __getitem__(self, k):
            return fake_flags

    ep._LAST_GOOD = None
    with patch.object(ep, "db", _DB()):
        good = await ep.get_policy()
        assert good["crypto"]["enabled"] is True
        assert good["crypto"]["sl_pct"] == 2.5

        # Atlas dies mid-session → last-known-good, NOT enabled=False.
        fake_flags.find_one = AsyncMock(side_effect=RuntimeError("atlas timeout"))
        stale = await ep.get_policy()
        assert stale["crypto"]["enabled"] is True, (
            "outage must not disable exits for open positions"
        )
        assert stale["crypto"]["sl_pct"] == 2.5
        assert stale.get("_stale") is True
    ep._LAST_GOOD = None


@pytest.mark.asyncio
async def test_outage_before_first_load_still_falls_back_to_defaults():
    fake_flags = AsyncMock()
    fake_flags.find_one = AsyncMock(side_effect=RuntimeError("atlas down"))

    class _DB(dict):
        def __getitem__(self, k):
            return fake_flags

    ep._LAST_GOOD = None
    with patch.object(ep, "db", _DB()):
        p = await ep.get_policy()
        assert p["crypto"]["enabled"] is True  # 2026-08 always-on default pre-first-load
    ep._LAST_GOOD = None
