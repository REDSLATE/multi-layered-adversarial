"""Webull pct-of-buying-power override — Mongo-backed runtime dial.

2026-06-23 — operator hit `WEBULL_NOTIONAL_ABOVE_CAP` blocking equity
trades. The default 10% × $470.96 BP = $47.10 max/order, clipping
$100 intents. Operator needed a phone-friendly knob to bump the pct
without a redeploy.

This file pins:
  * Precedence: Mongo override > env > default
  * Caching: refresh writes the in-memory cache; sync reader honors
    it for 5s TTL (× 30 stale-cache multiplier matches the existing
    floor override pattern).
  * Clamping: anything outside (0, 1.0] reverts to the next source.
"""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, "/app/backend")


@pytest.fixture
async def clean_state():
    """Each test starts with no Mongo override doc + a cold cache.
    Async fixture so we share the test runner's event loop with Motor's
    connection pool (rather than spinning up + tearing down loops via
    `asyncio.run` per test, which breaks Motor's pooling)."""
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    from db import db
    from shared.broker.webull_caps import _PCT_FLAG_DOC_ID
    import shared.broker.webull_caps as wc

    await db["runtime_flags"].delete_one({"_id": _PCT_FLAG_DOC_ID})
    wc._CACHED_PCT_OVERRIDE = None
    wc._CACHED_PCT_TS = 0.0
    yield db
    await db["runtime_flags"].delete_one({"_id": _PCT_FLAG_DOC_ID})
    wc._CACHED_PCT_OVERRIDE = None
    wc._CACHED_PCT_TS = 0.0


def test_default_when_no_override_no_env():
    """Sync test — never touches Mongo, so doesn't need the async
    fixture. Just verifies the env-less default."""
    from shared.broker.webull_caps import (
        DEFAULT_PCT_OF_BUYING_POWER, webull_pct_of_buying_power,
    )
    import shared.broker.webull_caps as wc
    wc._CACHED_PCT_OVERRIDE = None
    wc._CACHED_PCT_TS = 0.0
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("WEBULL_PCT_OF_BUYING_POWER", None)
        assert webull_pct_of_buying_power() == pytest.approx(
            DEFAULT_PCT_OF_BUYING_POWER
        )


def test_env_wins_when_no_mongo_override():
    """No Mongo override yet → env value should be picked up."""
    from shared.broker.webull_caps import webull_pct_of_buying_power
    import shared.broker.webull_caps as wc
    wc._CACHED_PCT_OVERRIDE = None
    wc._CACHED_PCT_TS = 0.0
    with patch.dict(os.environ, {"WEBULL_PCT_OF_BUYING_POWER": "0.15"}):
        assert webull_pct_of_buying_power() == pytest.approx(0.15)


@pytest.mark.asyncio
async def test_mongo_override_wins_over_env(clean_state):
    """Operator-pinned: Mongo override MUST win over env so the phone
    dial isn't second-class to deploy config."""
    db = clean_state
    from shared.broker.webull_caps import (
        _PCT_FLAG_DOC_ID, refresh_webull_pct_cache,
        webull_pct_of_buying_power,
    )
    await db["runtime_flags"].update_one(
        {"_id": _PCT_FLAG_DOC_ID},
        {"$set": {"enabled": True, "pct": 0.30}},
        upsert=True,
    )
    await refresh_webull_pct_cache()
    with patch.dict(os.environ, {"WEBULL_PCT_OF_BUYING_POWER": "0.05"}):
        # Mongo says 0.30, env says 0.05 — Mongo wins.
        assert webull_pct_of_buying_power() == pytest.approx(0.30)


@pytest.mark.asyncio
async def test_disabled_mongo_doc_falls_back_to_env(clean_state):
    """If the Mongo doc exists but `enabled` is False, the override is
    inactive and the env value should be used. Matches the `/clear`
    endpoint's behavior."""
    db = clean_state
    from shared.broker.webull_caps import (
        _PCT_FLAG_DOC_ID, refresh_webull_pct_cache,
        webull_pct_of_buying_power,
    )
    await db["runtime_flags"].update_one(
        {"_id": _PCT_FLAG_DOC_ID},
        {"$set": {"enabled": False, "pct": 0.30}},
        upsert=True,
    )
    await refresh_webull_pct_cache()
    with patch.dict(os.environ, {"WEBULL_PCT_OF_BUYING_POWER": "0.20"}):
        assert webull_pct_of_buying_power() == pytest.approx(0.20)


@pytest.mark.asyncio
async def test_mongo_override_out_of_range_reverts_to_env(clean_state):
    """Pct must be in (0, 1.0]. Anything outside that is nonsensical
    and the override should NOT be honored — fall back to env."""
    db = clean_state
    from shared.broker.webull_caps import (
        _PCT_FLAG_DOC_ID, refresh_webull_pct_cache,
        webull_pct_of_buying_power,
    )
    await db["runtime_flags"].update_one(
        {"_id": _PCT_FLAG_DOC_ID},
        {"$set": {"enabled": True, "pct": 1.5}},  # > 1.0
        upsert=True,
    )
    await refresh_webull_pct_cache()
    with patch.dict(os.environ, {"WEBULL_PCT_OF_BUYING_POWER": "0.20"}):
        # The 1.5 override is invalid → env's 0.20 should be used.
        assert webull_pct_of_buying_power() == pytest.approx(0.20)


@pytest.mark.asyncio
async def test_concrete_scenario_operator_unblocks_amzn(clean_state):
    """Exact scenario from prod: $470.96 BP × default 10% = $47.10
    max → blocks the $100 AMZN intent. Operator bumps pct to 0.25
    via Mongo override → cap becomes $117.74 → intent passes."""
    db = clean_state
    from shared.broker.webull_caps import (
        _PCT_FLAG_DOC_ID, refresh_webull_pct_cache,
        webull_pct_of_buying_power,
    )
    BUYING_POWER = 470.96
    INTENT_USD = 100.00

    # Before override — 10% × 470.96 = 47.10, blocks $100.
    pct_before = webull_pct_of_buying_power()
    cap_before = BUYING_POWER * pct_before
    assert cap_before < INTENT_USD, (
        f"Sanity: default pct ({pct_before}) should clip $100 intent. "
        f"Got cap=${cap_before:.2f}"
    )

    # Apply override.
    await db["runtime_flags"].update_one(
        {"_id": _PCT_FLAG_DOC_ID},
        {"$set": {"enabled": True, "pct": 0.25}},
        upsert=True,
    )
    await refresh_webull_pct_cache()

    # After override — 25% × 470.96 = 117.74, $100 passes.
    pct_after = webull_pct_of_buying_power()
    cap_after = BUYING_POWER * pct_after
    assert pct_after == pytest.approx(0.25)
    assert cap_after >= INTENT_USD, (
        f"After 0.25 override, cap should clear $100 intent. "
        f"Got cap=${cap_after:.2f}"
    )
