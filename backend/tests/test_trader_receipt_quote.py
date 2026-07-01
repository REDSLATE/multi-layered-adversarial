"""Test that receipts carry the L1 quote provenance block.

Verifies the wiring added 2026-07-02:
  main.py → snapshot spread.latest(symbol) at cycle-start
         → overlay onto brain-input `data`
         → pass `quote=quote_prov` to audit.write_receipt
  audit.py → stamp receipt row with the full quote block
  store.py → JSONL + SQLite receipt preserves the block
"""
from __future__ import annotations

import asyncio
import json
import sys

import pytest

sys.path.insert(0, "/app")

from trader import audit, spread, store  # noqa: E402


@pytest.fixture()
def fresh_store(tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    store.init(
        str(tmp_path / "executions.sqlite"),
        str(tmp_path / "jsonl"),
    )
    spread._latest.clear()
    yield tmp_path
    store.close()
    loop.close()
    asyncio.set_event_loop(None)


@pytest.mark.asyncio
async def test_receipt_carries_quote_provenance(fresh_store, tmp_path):
    """A receipt written with a `quote` block must preserve every
    field the operator asked to see (quote_source, quote_age_ms,
    bid, ask, spread_bps, last_price, l1_stale)."""
    quote_prov = {
        "quote_source": "webull_mqtt",
        "quote_age_ms": 42,
        "bid": 423.00,
        "ask": 423.04,
        "spread_bps": 0.9456,
        "last_price": 423.02,
        "l1_stale": False,
    }
    await audit.write_receipt(
        db=None,  # audit no longer uses db — kept in signature
        cycle_id="test-cycle-1",
        lane="equity",
        symbol="TSLA",
        last_price=423.02,
        signals=[{"brain": "camino", "verdict": "BUY", "confidence": 0.72}],
        chosen={"brain": "camino", "verdict": "BUY", "confidence": 0.72},
        seats={"executor": "camino", "strategist": "barracuda"},
        angels={"executor": "Paschar"},
        risk_verdict={"ok": True, "reason": "ok"},
        quote=quote_prov,
    )
    # Read back from SQLite via the store's public API
    rows = store.recent_receipts(limit=5)
    assert rows, "expected at least one receipt"
    r = rows[0]
    assert r["quote"] == quote_prov, (
        f"receipt lost/mangled the quote block: got {r.get('quote')}"
    )
    # And the JSONL truth tape too
    jsonl_files = list((tmp_path / "jsonl").glob("*.jsonl"))
    assert jsonl_files, "expected a receipts.jsonl file"
    lines = jsonl_files[0].read_text().strip().split("\n")
    parsed = [json.loads(x) for x in lines if x]
    receipt_row = next(
        (x for x in parsed if x.get("cycle_id") == "test-cycle-1"), None,
    )
    assert receipt_row is not None
    assert receipt_row["quote"]["quote_source"] == "webull_mqtt"
    assert receipt_row["quote"]["quote_age_ms"] == 42


@pytest.mark.asyncio
async def test_receipt_defaults_quote_block_when_omitted(fresh_store):
    """Legacy callers that don't pass `quote=` should still get a
    stable schema (all keys present, all values None-ish)."""
    await audit.write_receipt(
        db=None,
        cycle_id="test-cycle-2",
        lane="equity",
        symbol="TSLA",
        last_price=None,
        signals=[], chosen=None, seats={}, angels={},
        risk_verdict={"reason": "no_executor_signal"},
    )
    rows = store.recent_receipts(limit=5)
    r = next((x for x in rows if x["cycle_id"] == "test-cycle-2"), None)
    assert r is not None
    assert "quote" in r
    for k in ("quote_source", "quote_age_ms", "bid", "ask",
              "spread_bps", "last_price", "l1_stale"):
        assert k in r["quote"], f"missing default key: {k}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
