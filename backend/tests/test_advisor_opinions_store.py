"""Advisor opinions storage tests — TTL semantics, window collection."""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "/app/backend")

from shared.advisor_opinions import (  # noqa: E402
    COLLECTION, collect_for, ensure_indexes, store_opinion,
)


def _fake_intent(brain: str, symbol: str, action: str = "BUY",
                 confidence: float = 0.7) -> dict:
    return {
        "intent_id": str(uuid.uuid4()),
        "stack_canonical": brain,
        "symbol": symbol,
        "lane": "equity",
        "action": action,
        "confidence": confidence,
        "rationale": f"{brain} {action} {symbol}",
        "evidence": {"market_regime": "trend"},
    }


@pytest.mark.asyncio
async def test_store_and_collect_one_opinion():
    from db import db
    symbol = f"OPIN{uuid.uuid4().hex[:6].upper()}"
    try:
        await ensure_indexes(db)
        oid = await store_opinion(db, _fake_intent("camino", symbol))
        assert oid is not None
        opinions = await collect_for(db, symbol, "equity", window_sec=60)
        assert len(opinions) == 1
        assert opinions[0].brain == "camino"
        assert opinions[0].symbol == symbol
        assert opinions[0].market_regime == "trend"
    finally:
        await db[COLLECTION].delete_many({"symbol": symbol})


@pytest.mark.asyncio
async def test_collect_returns_one_opinion_per_brain():
    """A chatty brain emitting twice in the window must only count
    once — newest opinion wins."""
    import asyncio
    from db import db
    symbol = f"DUPE{uuid.uuid4().hex[:6].upper()}"
    try:
        await store_opinion(db, _fake_intent("camino", symbol, "HOLD", 0.40))
        # 50ms gap so the second insert's emitted_at timestamp is
        # strictly greater than the first's — Mongo timestamp
        # resolution + a same-tick race could otherwise reorder.
        await asyncio.sleep(0.05)
        await store_opinion(db, _fake_intent("camino", symbol, "BUY",  0.85))
        await store_opinion(db, _fake_intent("hellcat", symbol, "BUY", 0.70))
        opinions = await collect_for(db, symbol, "equity")
        brains = [o.brain for o in opinions]
        assert sorted(brains) == ["camino", "hellcat"]
        camino_op = next(o for o in opinions if o.brain == "camino")
        assert camino_op.action == "BUY"
        assert camino_op.confidence == 0.85
    finally:
        await db[COLLECTION].delete_many({"symbol": symbol})


@pytest.mark.asyncio
async def test_window_excludes_old_opinions():
    from db import db
    symbol = f"OLD{uuid.uuid4().hex[:6].upper()}"
    try:
        # Manually insert an opinion 5 minutes old — well outside the
        # default 60-sec window.
        old_ts = datetime.now(timezone.utc) - timedelta(seconds=300)
        await db[COLLECTION].insert_one({
            "brain": "camino", "symbol": symbol, "lane": "equity",
            "action": "BUY", "confidence": 0.80,
            "emitted_at": old_ts,
            "expires_at": old_ts + timedelta(seconds=600),
        })
        opinions = await collect_for(db, symbol, "equity", window_sec=60)
        assert opinions == []
        # Widen the window — now it should be included.
        opinions_wide = await collect_for(db, symbol, "equity", window_sec=600)
        assert len(opinions_wide) == 1
    finally:
        await db[COLLECTION].delete_many({"symbol": symbol})


@pytest.mark.asyncio
async def test_store_missing_required_fields_is_silent_noop():
    from db import db
    # Missing symbol → should return None, NOT raise. Opinion
    # storage failures must never take down ingest.
    bad = {"intent_id": "x", "stack_canonical": "camino"}
    assert await store_opinion(db, bad) is None
