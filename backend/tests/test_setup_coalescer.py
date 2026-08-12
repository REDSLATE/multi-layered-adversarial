"""Setup-aware intent coalescing tests — stack-level dedupe doctrine."""
import uuid

import pytest

from db import db
from shared import setup_coalescer as sc


def _doc(symbol, action="BUY", brain="gto", conf=0.6, price=100.0, lane="crypto"):
    return {"intent_id": uuid.uuid4().hex, "lane": lane, "symbol": symbol,
            "action": action, "stack": brain, "stack_canonical": brain,
            "confidence": conf, "signal_price": price}


@pytest.fixture
async def _clean():
    sym = f"TEST{uuid.uuid4().hex[:6].upper()}/USD"
    yield sym
    await db[sc.COLLECTION].delete_many({"symbol": sym})
    await db["shared_intents"].delete_many({"symbol": sym})


@pytest.mark.asyncio
async def test_first_intent_never_blocked(_clean):
    doc = _doc(_clean)
    assert await sc.coalesce_or_register(doc) is None
    assert doc["setup_role"] == "primary"
    assert doc["setup_id"].startswith(f"crypto:{_clean}:BUY:")


@pytest.mark.asyncio
async def test_repeat_coalesces_across_brains(_clean):
    d1 = _doc(_clean, brain="gto", conf=0.6)
    assert await sc.coalesce_or_register(d1) is None
    # different brain, same lane+symbol+side → SAME setup (stack-level)
    d2 = _doc(_clean, brain="hellcat", conf=0.8)
    res = await sc.coalesce_or_register(d2)
    assert res is not None and res["coalesced"]
    assert res["primary_intent_id"] == d1["intent_id"]
    assert res["signal_count"] == 2
    setup = await db[sc.COLLECTION].find_one({"setup_id": d1["setup_id"]})
    assert setup["max_confidence"] == 0.8
    assert set(setup["contributions"]) == {"gto", "hellcat"}
    assert setup["contributions"]["hellcat"]["signal_count"] == 1
    primary = await db["shared_intents"].find_one({"intent_id": d1["intent_id"]})
    # primary doc bump happens even though it was never inserted here;
    # update_one on missing doc is a no-op — verify no crash
    assert primary is None or primary.get("repeat_count") == 1


@pytest.mark.asyncio
async def test_price_drift_starts_new_setup(_clean):
    d1 = _doc(_clean, price=100.0)
    assert await sc.coalesce_or_register(d1) is None
    d2 = _doc(_clean, price=107.0)  # > 5% drift → new structure
    assert await sc.coalesce_or_register(d2) is None
    assert d2["setup_id"] != d1["setup_id"]
    old = await db[sc.COLLECTION].find_one({"setup_id": d1["setup_id"]})
    assert old["status"] == "terminated"
    assert old["terminated_reason"] == "price_structure_change"


@pytest.mark.asyncio
async def test_exits_never_coalesced(_clean):
    assert await sc.coalesce_or_register(_doc(_clean, action="SELL")) is None
    assert await sc.coalesce_or_register(_doc(_clean, action="SELL")) is None
    assert await db[sc.COLLECTION].count_documents({"symbol": _clean}) == 0
