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
        "$or": [
            {"_id": {"$in": [_CAPS_FLAG_DOC_ID, _DAILY_SPEND_RESET_DOC_ID]}},
            {"_id": {"$regex": f"^{_DAILY_SPEND_RESET_DOC_ID}:"}},
        ],
    })
    await db["execution_receipts"].delete_many({
        "_test_marker": "daily_spend_reset_test",
    })
    yield db
    # Cleanup AFTER the test so other tests don't inherit our docs.
    await db["runtime_flags"].delete_many({
        "$or": [
            {"_id": {"$in": [_CAPS_FLAG_DOC_ID, _DAILY_SPEND_RESET_DOC_ID]}},
            {"_id": {"$regex": f"^{_DAILY_SPEND_RESET_DOC_ID}:"}},
        ],
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


# ── per-brain reset: scoped wipe + breakdown ─────────────────────


async def _insert_receipt_with_brain(db, executed_at, notional, stack):
    """Like _insert_receipt but also stamps the brain id (Mongo
    field is `stack`, same as production receipts)."""
    await db["execution_receipts"].insert_one({
        "executed_at": executed_at.isoformat(),
        "side": "BUY",
        "notional_usd": notional,
        "stack": stack,
        "_test_marker": "daily_spend_reset_test",
    })


@pytest.mark.asyncio
async def test_per_brain_reset_only_drops_that_brains_fills(clean_state):
    """Per-brain reset must wipe ONLY that brain's contribution to
    the global sum — other brains' fills still count.
    Use case: operator promotes Camino to hot. They want Camino's
    history reset so it gets fresh runway, but Barracuda's history
    must NOT vanish from audit visibility."""
    from shared.exposure_caps import (
        daily_spend_usd, daily_spend_per_brain,
        _daily_spend_reset_doc_id,
    )
    db = clean_state
    now = datetime.now(timezone.utc)
    # Pre-reset fills from two brains: Camino $300, Barracuda $500.
    # (Use canonical IDs directly — alias normalization is tested
    # separately below.)
    await _insert_receipt_with_brain(db, now - timedelta(hours=4), 300.0, "camino")
    await _insert_receipt_with_brain(db, now - timedelta(hours=4), 500.0, "barracuda")
    # Sanity: total $800.
    pre = await daily_spend_usd()
    assert pre == pytest.approx(800.0, abs=0.01)

    # Reset camino only.
    await db["runtime_flags"].update_one(
        {"_id": _daily_spend_reset_doc_id("camino")},
        {"$set": {"reset_at": now.isoformat(), "reset_by": "test"}},
        upsert=True,
    )
    # Total must drop by $300 (Camino's contribution) — NOT $800.
    post = await daily_spend_usd()
    assert post == pytest.approx(500.0, abs=0.01), (
        f"Per-brain reset must drop only that brain's contribution. "
        f"Expected $500 (Barracuda remains), got ${post:.2f}"
    )
    breakdown = await daily_spend_per_brain()
    assert breakdown.get("camino", 0.0) == pytest.approx(0.0, abs=0.01)
    assert breakdown.get("barracuda", 0.0) == pytest.approx(500.0, abs=0.01)


@pytest.mark.asyncio
async def test_per_brain_reset_honors_legacy_aliases(clean_state):
    """DB receipts may carry the legacy slot code (`stack="camaro"`)
    even after the rename. A reset issued against the canonical
    name (`brain="barracuda"`) MUST match those legacy-tagged
    receipts via the LEGACY_TO_CANONICAL map. Otherwise the
    operator's reset would silently fail to drop the right rows."""
    from shared.exposure_caps import (
        daily_spend_usd, daily_spend_per_brain,
        _daily_spend_reset_doc_id,
    )
    db = clean_state
    now = datetime.now(timezone.utc)
    # Fill tagged with the legacy slot code on stack.
    await _insert_receipt_with_brain(db, now - timedelta(hours=2), 400.0, "camaro")
    # Reset the canonical (barracuda).
    await db["runtime_flags"].update_one(
        {"_id": _daily_spend_reset_doc_id("barracuda")},
        {"$set": {"reset_at": now.isoformat(), "reset_by": "test"}},
        upsert=True,
    )
    total = await daily_spend_usd()
    assert total == pytest.approx(0.0, abs=0.01), (
        f"Canonical per-brain reset must drop legacy-tagged fills "
        f"via alias mapping. Got ${total:.2f}"
    )
    # Breakdown bucket should be canonical too.
    breakdown = await daily_spend_per_brain()
    assert "camaro" not in breakdown, (
        f"Breakdown buckets must be canonical IDs. Got {breakdown}"
    )


@pytest.mark.asyncio
async def test_global_and_per_brain_resets_compose(clean_state):
    """Global reset moves the floor; per-brain reset adds an extra
    exclusion ON TOP. Both must compose without one cancelling the
    other."""
    from shared.exposure_caps import (
        daily_spend_usd, _DAILY_SPEND_RESET_DOC_ID,
        _daily_spend_reset_doc_id,
    )
    db = clean_state
    now = datetime.now(timezone.utc)
    # Older Barracuda fill before any reset.
    await _insert_receipt_with_brain(db, now - timedelta(hours=10), 200.0, "barracuda")
    # Global reset at -8h: clears the older Barracuda fill.
    await db["runtime_flags"].update_one(
        {"_id": _DAILY_SPEND_RESET_DOC_ID},
        {"$set": {
            "reset_at": (now - timedelta(hours=8)).isoformat(),
            "reset_by": "test",
        }},
        upsert=True,
    )
    # Post-global-reset fills from both brains.
    await _insert_receipt_with_brain(db, now - timedelta(hours=4), 100.0, "camino")
    await _insert_receipt_with_brain(db, now - timedelta(hours=4), 150.0, "barracuda")
    # Sanity: with only global reset, post-reset fills sum to $250.
    pre_per_brain = await daily_spend_usd()
    assert pre_per_brain == pytest.approx(250.0, abs=0.01)
    # Now also reset camino specifically.
    await db["runtime_flags"].update_one(
        {"_id": _daily_spend_reset_doc_id("camino")},
        {"$set": {"reset_at": now.isoformat(), "reset_by": "test"}},
        upsert=True,
    )
    # Camino's $100 drops out → $150 left.
    post = await daily_spend_usd()
    assert post == pytest.approx(150.0, abs=0.01), (
        f"Global + per-brain resets must compose. Expected $150, "
        f"got ${post:.2f}"
    )
