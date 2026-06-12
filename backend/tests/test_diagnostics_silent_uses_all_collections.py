"""Test for the SILENT-badge prod bug (2026-02-19, final+6).

Operator reported: production shows all 4 brains as `SILENT — alive
but no decisions` while the Decisions Feed RIGHT BELOW shows the same
brains posting `gate_pass` entries every second. The Brain Health
panel meanwhile says `opinion: fresh 18s`. Multiple sources of truth
disagreeing.

Root cause: `shared/diagnostics.py::_last_receipt_ts` queried ONLY the
legacy `shared_receipts` collection. Modern in-process brains write
to:
  * `shared_brain_opinions.posted_at`  (every intent → opinion)
  * `shared_intents.ingest_ts`          (every intent)
  * `<brain>_decision_log.timestamp`    (per-brain audit)

But not `shared_receipts` (only the authority-call mirror in
`shared/opinions.py` writes to it, and only when an opinion carries
`evidence.authority_call`). So a brain firing intents every second
looked SILENT for 13 days.

Fix: take the MAX timestamp across ALL decision-producing
collections. If ANY of them is fresh, the brain is not silent.

This test seeds an opinion (NO legacy receipt) and asserts the
freshness check returns the opinion's timestamp — what the prod
brains actually do today.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv("/app/backend/.env")

from db import db  # noqa: E402
from namespaces import (  # noqa: E402
    SHARED_OPINIONS, SHARED_RECEIPTS, SHARED_INTENTS,
    ALPHA_DECISION_LOG,
)
from shared.diagnostics import _last_receipt_ts, _effective_tier  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────


def _now_iso(delta_sec: float = 0) -> str:
    from datetime import timedelta
    return (
        datetime.now(timezone.utc) + timedelta(seconds=delta_sec)
    ).isoformat()


@pytest.fixture
def isolated_runtime():
    """Use a unique runtime id per test so we don't see other rows."""
    rt = f"silent-test-{uuid.uuid4().hex[:10]}"
    yield rt
    # Cleanup across all collections we touch.
    async def _cleanup():
        await db[SHARED_RECEIPTS].delete_many({"runtime": rt})
        await db[SHARED_OPINIONS].delete_many({"runtime": rt})
        await db[SHARED_INTENTS].delete_many({"stack": rt})
    asyncio.get_event_loop().run_until_complete(_cleanup())


# ── core regression: opinion alone proves the brain isn't silent ──


@pytest.mark.asyncio
async def test_fresh_opinion_alone_keeps_brain_out_of_silent(isolated_runtime):
    """Operator's exact prod scenario: brain posts opinions every few
    seconds via the in-process runner. NO writes to shared_receipts.
    The freshness check MUST find the opinion and report a fresh
    timestamp — not return None and trip SILENT."""
    rt = isolated_runtime
    posted_at = _now_iso()
    await db[SHARED_OPINIONS].insert_one({
        "opinion_id": str(uuid.uuid4()),
        "runtime": rt,
        "topic": "symbol:NVDA",
        "stance": "long",
        "confidence": 0.7,
        "posted_at": posted_at,
    })
    # No row in shared_receipts. No intent. No decision log.
    ts = await _last_receipt_ts(rt)
    assert ts == posted_at, (
        "freshness check must find the opinion timestamp when "
        "no legacy receipt exists"
    )


@pytest.mark.asyncio
async def test_fresh_intent_alone_keeps_brain_out_of_silent(isolated_runtime):
    """Brain that posts intents but no opinions (corner case — e.g.,
    HOLD intents that don't trigger a directional opinion) still
    must NOT be marked SILENT."""
    rt = isolated_runtime
    ingest_ts = _now_iso()
    await db[SHARED_INTENTS].insert_one({
        "intent_id": str(uuid.uuid4()),
        "stack": rt,
        "action": "HOLD",
        "symbol": "AAPL",
        "lane": "equity",
        "ingest_ts": ingest_ts,
    })
    ts = await _last_receipt_ts(rt)
    assert ts == ingest_ts


@pytest.mark.asyncio
async def test_picks_max_across_collections(isolated_runtime):
    """When multiple collections have entries, return the LATEST."""
    rt = isolated_runtime
    old_ts = _now_iso(-3600)          # 1h ago
    mid_ts = _now_iso(-60)            # 1m ago
    new_ts = _now_iso(-5)             # 5s ago

    await db[SHARED_RECEIPTS].insert_one({
        "runtime": rt, "timestamp": old_ts, "action": "authority_call",
    })
    await db[SHARED_OPINIONS].insert_one({
        "opinion_id": str(uuid.uuid4()),
        "runtime": rt, "posted_at": mid_ts,
        "topic": "symbol:X", "stance": "long", "confidence": 0.5,
    })
    await db[SHARED_INTENTS].insert_one({
        "intent_id": str(uuid.uuid4()),
        "stack": rt, "ingest_ts": new_ts,
        "action": "BUY", "symbol": "X", "lane": "equity",
    })

    ts = await _last_receipt_ts(rt)
    assert ts == new_ts, (
        f"must pick the latest across all 3 collections (got {ts!r})"
    )


@pytest.mark.asyncio
async def test_no_artifact_returns_none(isolated_runtime):
    """A brain that has NEVER written to any decision collection is
    correctly classified as silent (the SILENT tier's original
    purpose — a brand-new pod that's heartbeating but never wrote
    anything is genuinely silent)."""
    rt = isolated_runtime
    ts = await _last_receipt_ts(rt)
    assert ts is None


# ── effective_tier wiring sanity ──────────────────────────────────


def test_effective_tier_silent_only_with_stale_or_missing_receipt():
    """The doctrine: fresh heartbeat + recent receipt = ok.
    Fresh heartbeat + stale receipt OR no receipt = silent."""
    # Fresh on both axes → ok.
    assert _effective_tier("ok", 30.0) == "ok"
    # Fresh hb, stale receipt → silent.
    assert _effective_tier("ok", 9999.0) == "silent"
    # Fresh hb, no receipt → silent.
    assert _effective_tier("ok", None) == "silent"
    # Dead heartbeat passes through regardless of receipt.
    assert _effective_tier("dead", 30.0) == "dead"
    assert _effective_tier("stale", None) == "stale"
