"""Daily-spend reset — operator's "start over" button for the rolling
24h exposure cap window.

The doctrine: the reset writes a baseline timestamp; the 24h sum
is computed as `sum(notional WHERE executed_at > max(window_start,
reset_at))`. Audit rows in `execution_receipts` are NEVER deleted.

This file pins the offset math, the natural age-out, and the
endpoint contract.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

sys.path.insert(0, "/app/backend")


@pytest.fixture
async def clean_state(monkeypatch):
    """Wipe both the cap-override and daily-spend-reset docs plus
    any leftover receipts created by other tests. Each test starts
    from a clean slate so the offset math is unambiguous."""
    import os
    os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
    os.environ.setdefault("DB_NAME", "test_db_caps_reset")
    from dotenv import load_dotenv
    load_dotenv("/app/backend/.env")
    from db import db
    from shared.exposure_caps import (
        _CAPS_FLAG_DOC_ID,
        _DAILY_SPEND_RESET_DOC_ID,
    )
    await db["runtime_flags"].delete_many({
        "_id": {"$in": [_CAPS_FLAG_DOC_ID, _DAILY_SPEND_RESET_DOC_ID]},
    })
    await db["execution_receipts"].delete_many({
        "_test_marker": "daily_spend_reset_test",
    })
    yield db
    # Cleanup AFTER the test so other tests don't inherit our docs.
    await db["runtime_flags"].delete_many({
        "_id": {"$in": [_CAPS_FLAG_DOC_ID, _DAILY_SPEND_RESET_DOC_ID]},
    })
    await db["execution_receipts"].delete_many({
        "_test_marker": "daily_spend_reset_test",
    })


async def _insert_receipt(db, executed_at: datetime, notional: float):
    """Drop a marker-tagged receipt so the cleanup fixture can pick
    it up without disturbing real audit rows."""
    await db["execution_receipts"].insert_one({
        "executed_at": executed_at.isoformat(),
        "side": "BUY",
        "notional_usd": notional,
        "_test_marker": "daily_spend_reset_test",
    })


# ── happy path: reset writes baseline → spend goes to 0 ──────────


@pytest.mark.asyncio
async def test_reset_drops_pre_reset_receipts_from_sum(clean_state):
    """A reset RIGHT NOW must drop everything that happened before
    it. Receipts AFTER the reset still count."""
    from shared.exposure_caps import daily_spend_usd
    db = clean_state
    now = datetime.now(timezone.utc)
    # Two fills in the recent past — together $500.
    await _insert_receipt(db, now - timedelta(hours=2), 300.0)
    await _insert_receipt(db, now - timedelta(hours=1), 200.0)

    # Sanity: with NO reset, both count → $500.
    pre_reset = await daily_spend_usd()
    assert pre_reset == pytest.approx(500.0, abs=0.01), (
        f"Without a reset, the rolling window must sum both fills. "
        f"Got ${pre_reset:.2f}"
    )

    # Write a reset doc at "now". Both fills are now in the past.
    from shared.exposure_caps import _DAILY_SPEND_RESET_DOC_ID
    await db["runtime_flags"].update_one(
        {"_id": _DAILY_SPEND_RESET_DOC_ID},
        {"$set": {"reset_at": now.isoformat(), "reset_by": "test"}},
        upsert=True,
    )
    post_reset = await daily_spend_usd()
    assert post_reset == pytest.approx(0.0, abs=0.01), (
        f"After reset, pre-reset fills must NOT count. "
        f"Got ${post_reset:.2f}"
    )


@pytest.mark.asyncio
async def test_receipts_after_reset_still_count(clean_state):
    """The reset is a baseline, not a permanent cap-bypass — fills
    AFTER the reset still accumulate against the cap as normal."""
    from shared.exposure_caps import daily_spend_usd, _DAILY_SPEND_RESET_DOC_ID
    db = clean_state
    now = datetime.now(timezone.utc)
    # Pre-reset fill ($500) — should NOT count.
    await _insert_receipt(db, now - timedelta(hours=3), 500.0)
    # Reset 2 hours ago.
    await db["runtime_flags"].update_one(
        {"_id": _DAILY_SPEND_RESET_DOC_ID},
        {"$set": {
            "reset_at": (now - timedelta(hours=2)).isoformat(),
            "reset_by": "test",
        }},
        upsert=True,
    )
    # Post-reset fills — $80 + $20 = $100.
    await _insert_receipt(db, now - timedelta(hours=1), 80.0)
    await _insert_receipt(db, now - timedelta(minutes=15), 20.0)

    total = await daily_spend_usd()
    assert total == pytest.approx(100.0, abs=0.01), (
        f"Only post-reset fills should sum. Got ${total:.2f}"
    )


# ── natural age-out: an old reset has no effect ──────────────────


@pytest.mark.asyncio
async def test_old_reset_ages_out_naturally(clean_state):
    """If the reset was MORE than 24h ago, all pre-reset receipts
    are outside the rolling window anyway — the reset doc has no
    effect on the math. The system is self-cleaning."""
    from shared.exposure_caps import daily_spend_usd, _DAILY_SPEND_RESET_DOC_ID
    db = clean_state
    now = datetime.now(timezone.utc)
    # Reset 48h ago — way outside the 24h window.
    await db["runtime_flags"].update_one(
        {"_id": _DAILY_SPEND_RESET_DOC_ID},
        {"$set": {
            "reset_at": (now - timedelta(hours=48)).isoformat(),
            "reset_by": "test",
        }},
        upsert=True,
    )
    # Fill 5 hours ago — well inside the window AND after the reset.
    await _insert_receipt(db, now - timedelta(hours=5), 250.0)

    total = await daily_spend_usd()
    assert total == pytest.approx(250.0, abs=0.01), (
        "An aged-out reset must not interfere with normal rolling "
        f"behavior. Got ${total:.2f}"
    )


# ── no reset doc → unchanged behavior ────────────────────────────


@pytest.mark.asyncio
async def test_no_reset_doc_uses_normal_24h_window(clean_state):
    """If no reset has ever been written, the function MUST behave
    exactly as it did before this feature was added. Regression
    guard for the baseline-or-window-start `max()` logic."""
    from shared.exposure_caps import daily_spend_usd
    db = clean_state
    now = datetime.now(timezone.utc)
    await _insert_receipt(db, now - timedelta(hours=1), 75.0)
    await _insert_receipt(db, now - timedelta(hours=10), 175.0)

    total = await daily_spend_usd()
    assert total == pytest.approx(250.0, abs=0.01)
